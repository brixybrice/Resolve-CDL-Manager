#! /usr/bin/env python
# -*- coding: utf-8 -*-

import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

win_id = "com.be4post.cdlmanager.main"

# Resolve runs this script via exec(), so `__file__` is not defined here. Fall back
# to the known Workflow Integration Plugins install location on macOS.
DEFAULT_PLUGIN_DIR = (
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
    "Workflow Integration Plugins"
)


def get_script_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        pass

    try:
        candidate = os.path.dirname(os.path.abspath(sys.argv[0]))
        if candidate:
            return candidate
    except Exception:
        pass

    return DEFAULT_PLUGIN_DIR


# ---------------------------------------------------------------------------
# Export: pure ALE -> CDL logic (port of the "ALE to CDL Converter" script.js).
# No `bmd` dependency, so these can be imported and tested outside Resolve.
# ---------------------------------------------------------------------------

EXPORT_NAMING_SOURCES = [
    ("auto", "Auto (Name → Reel)"),
    ("name", "Clip Name Only"),
    ("reel", "Reel Name Only"),
]

DEFAULT_SATURATION = "1.0000"

def print_log(message, is_error=False):
    print(f"{'ERROR ' if is_error else ''}{message}")


SOP_PATTERN = re.compile(r"\(([\d.\s-]+)\)\(([\d.\s-]+)\)\(([\d.\s-]+)\)")


def parse_ale(text):
    """Return (columns, data_lines) from ALE text, Resolve or Daylight flavour."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    stripped = [line.strip() for line in lines]

    is_resolve_format = "Heading" in stripped and "Column" in stripped and "Data" in stripped

    if is_resolve_format:
        sections = {"heading": [], "column": [], "data": []}
        current = None
        for raw, line in zip(lines, stripped):
            if line == "Heading":
                current = "heading"
                continue
            if line == "Column":
                current = "column"
                continue
            if line == "Data":
                current = "data"
                continue
            if current:
                # Keep raw lines for data: stripping would drop leading empty fields.
                sections[current].append(raw)

        column_lines = [line for line in sections["column"] if line.strip()]
        if not column_lines:
            return None, None, "Error: ALE file doesn't have the expected format (missing Column section)"

        columns = column_lines[0].split("\t")
        data_lines = [line for line in sections["data"] if line.strip()]
        return columns, data_lines, "Detected DaVinci Resolve ALE format"

    header_index = next((i for i, line in enumerate(lines) if "\t" in line), -1)
    if header_index == -1:
        return None, None, "Error: Could not find column headers in ALE file"

    columns = lines[header_index].split("\t")
    data_lines = [line for line in lines[header_index + 1:] if line.strip()]
    return columns, data_lines, "Detected Daylight ALE format"


def find_columns(columns):
    """Map logical fields to column indices. Matching is case-insensitive since
    Resolve writes "Reel name" while other tools write "Reel Name"/"Reel"."""
    wanted = {
        "name": ("name",),
        "reel": ("reel", "reel name"),
        "asc_sop": ("asc_sop",),
        "asc_sat": ("asc_sat",),
        "lut_used": ("lut used",),
        "lut_nodes": ("lut nodes",),
    }
    indices = {key: -1 for key in wanted}
    for index, col in enumerate(columns):
        normalized = col.strip().lower()
        for key, names in wanted.items():
            if indices[key] == -1 and normalized in names:
                indices[key] = index
    return indices


def _field(fields, index):
    if index != -1 and len(fields) > index:
        return fields[index].strip()
    return ""


def extract_clips(columns, data_lines, naming_source, log=print_log):
    idx = find_columns(columns)

    if idx["asc_sop"] == -1:
        log("Error: ASC_SOP column not found in ALE file", True)
        return []
    if naming_source == "name" and idx["name"] == -1:
        log('Error: Name column not found in ALE file but "Clip Name Only" was selected', True)
        return []
    if naming_source == "reel" and idx["reel"] == -1:
        log('Error: Reel Name column not found in ALE file but "Reel Name Only" was selected', True)
        return []
    if naming_source == "auto" and idx["name"] == -1 and idx["reel"] == -1:
        log("Error: Neither Name nor Reel Name column found in ALE file", True)
        return []

    clips = []
    for line in data_lines:
        fields = line.split("\t")
        if len(fields) <= idx["asc_sop"]:
            continue

        clip_name = ""
        if naming_source in ("auto", "name"):
            clip_name = os.path.splitext(_field(fields, idx["name"]))[0]
        if (not clip_name and naming_source == "auto") or naming_source == "reel":
            clip_name = _field(fields, idx["reel"])

        if not clip_name:
            log(f"Warning: Could not determine clip name for line: {line[:50]}...", True)
            continue

        sop_match = SOP_PATTERN.search(fields[idx["asc_sop"]])
        if not sop_match:
            log(f"Warning: No valid ASC_SOP for {clip_name}, skipped", True)
            continue

        saturation = _field(fields, idx["asc_sat"]) or DEFAULT_SATURATION
        lut_info = _field(fields, idx["lut_used"]) or _field(fields, idx["lut_nodes"])

        clips.append({
            "name": clip_name,
            "slope": " ".join(sop_match.group(1).split()),
            "offset": " ".join(sop_match.group(2).split()),
            "power": " ".join(sop_match.group(3).split()),
            "saturation": saturation,
            "lut_info": lut_info,
        })
        log(f"Found clip: {clip_name} with CDL values{' and LUT: ' + lut_info if lut_info else ''}")

    return clips


def _cdl_values(clip):
    return (clip["slope"], clip["offset"], clip["power"], clip["saturation"])


def dedupe_clips(clips, log=print_log):
    """One entry per clip name. A clip used several times with identical values is
    written once; differing values get `_2`, `_3`… suffixes (in timeline order)."""
    seen = {}  # name -> list of distinct value tuples
    result = []
    for clip in clips:
        values = _cdl_values(clip)
        variants = seen.setdefault(clip["name"], [])
        if values in variants:
            continue
        variants.append(values)
        if len(variants) == 1:
            result.append(dict(clip))
        else:
            suffixed = dict(clip, name=f"{clip['name']}_{len(variants)}")
            log(f"Warning: {clip['name']} has different CDL values on the timeline, "
                f"writing {suffixed['name']}", True)
            result.append(suffixed)
    return result


def xml_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_cdl(clip, include_sop=True, include_sat=True):
    description = clip["name"]
    if clip.get("lut_info"):
        description += f" - {clip['lut_info']}"

    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ColorDecisionList xmlns="urn:ASC:CDL:v1.01">\n'
        "\t<ColorDecision>\n"
        "\t\t<ColorCorrection>"
    )

    if include_sop:
        content += (
            "\n\t\t\t<SOPNode>"
            f"\n\t\t\t\t<Description>{xml_escape(description)}</Description>"
            f"\n\t\t\t\t<Slope>{clip['slope']}</Slope>"
            f"\n\t\t\t\t<Offset>{clip['offset']}</Offset>"
            f"\n\t\t\t\t<Power>{clip['power']}</Power>"
            "\n\t\t\t</SOPNode>"
        )

    if include_sat:
        content += (
            "\n\t\t\t<SATNode>"
            f"\n\t\t\t\t<Saturation>{clip['saturation']}</Saturation>"
            "\n\t\t\t</SATNode>"
        )

    content += (
        "\n\t\t</ColorCorrection>"
        "\n\t</ColorDecision>"
        "\n</ColorDecisionList>\n"
    )
    return content


def safe_filename(name):
    return re.sub(r'[/\\:]', "_", name).strip()


def write_cdl_files(clips, output_dir, include_sop, include_sat, log=print_log, progress=None):
    """Write one .cdl per clip. Returns a list of {name, path, content, data}."""
    written = []
    total = len(clips)
    for n, clip in enumerate(clips, start=1):
        filename = f"{safe_filename(clip['name'])}.cdl"
        path = os.path.join(output_dir, filename)
        content = build_cdl(clip, include_sop, include_sat)
        overwritten = os.path.exists(path)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            log(f"Error writing {path}: {e}", True)
            continue
        log(f"{'Overwrote' if overwritten else 'Wrote'} {filename}")
        written.append({"name": filename, "path": path, "content": content, "data": clip})
        if progress:
            progress(n, total)
    return written


# ---------------------------------------------------------------------------
# Import settings
# ---------------------------------------------------------------------------

CDL_EXTENSIONS = (".cdl", ".cc", ".ccc")
DEFAULT_MATCH_CHARS = 10
TARGET_NODE_INDEX = 1

IMPORT_NAMING_SOURCES = [
    ("name", "Clip Name"),
    ("reel", "Reel Name"),
]

NEUTRAL = {
    "slope": "1.0000 1.0000 1.0000",
    "offset": "0.0000 0.0000 0.0000",
    "power": "1.0000 1.0000 1.0000",
    "saturation": "1.0000",
}


# ---------------------------------------------------------------------------
# Import: pure CDL parsing / matching. No `bmd` dependency, testable outside Resolve.
# ---------------------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child_text(parent, name):
    for el in parent.iter():
        if _local(el.tag) == name and el.text and el.text.strip():
            return " ".join(el.text.split())
    return ""


def parse_cdl_text(text, stem):
    """Return a list of {name, slope, offset, power, saturation} from .cdl/.cc/.ccc
    XML. A file holding a single ColorCorrection is named after the file (`stem`);
    with several, each one is named after its `id` attribute."""
    root = ET.fromstring(text)
    corrections = [el for el in root.iter() if _local(el.tag) == "ColorCorrection"]

    result = []
    for n, cc in enumerate(corrections, start=1):
        if len(corrections) == 1:
            name = stem
        else:
            name = (cc.get("id") or "").strip() or f"{stem}_{n}"
        result.append({
            "name": name,
            "slope": _child_text(cc, "Slope") or NEUTRAL["slope"],
            "offset": _child_text(cc, "Offset") or NEUTRAL["offset"],
            "power": _child_text(cc, "Power") or NEUTRAL["power"],
            "saturation": _child_text(cc, "Saturation") or NEUTRAL["saturation"],
        })
    return result


def load_cdl_folder(folder, log=print_log):
    cdls = []
    for filename in sorted(os.listdir(folder)):
        stem, ext = os.path.splitext(filename)
        if ext.lower() not in CDL_EXTENSIONS or filename.startswith("."):
            continue
        path = os.path.join(folder, filename)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                entries = parse_cdl_text(f.read(), stem)
        except Exception as e:
            log(f"Warning: cannot read {filename}: {e}", True)
            continue
        if not entries:
            log(f"Warning: no ColorCorrection in {filename}", True)
        for entry in entries:
            entry["file"] = filename
            cdls.append(entry)
    return cdls


VARIANT_PATTERN = re.compile(r"^(.*)_(\d+)$")


def split_variants(cdls):
    """Separate `name_2`, `name_3`… (written by CDLExport when one clip had several
    different CDLs) from the primary entries, when `name` itself is also present."""
    names = {c["name"] for c in cdls}
    primary, variants = [], []
    for c in cdls:
        m = VARIANT_PATTERN.match(c["name"])
        if m and m.group(1) in names:
            variants.append(c)
        else:
            primary.append(c)
    return primary, variants


def match_clips(clips, cdls, match_chars):
    """clips: list of {key, ...}. Exact name match first, then the first
    `match_chars` characters (0 disables the fallback). Returns (matches, unused)
    where each match is the clip dict plus `cdl` (or None) and `status`."""
    primary, variants = split_variants(cdls)
    exact = {}
    by_prefix = {}
    for c in primary:
        exact.setdefault(c["name"], c)
        if match_chars > 0:
            by_prefix.setdefault(c["name"][:match_chars], []).append(c)

    used = set()
    matches = []
    for clip in clips:
        key = clip["key"]
        cdl, status = None, "no CDL"
        if key in exact:
            cdl, status = exact[key], "exact"
        elif match_chars > 0 and key:
            candidates = by_prefix.get(key[:match_chars], [])
            if len(candidates) == 1:
                cdl, status = candidates[0], f"first {match_chars} chars"
            elif len(candidates) > 1:
                status = "ambiguous: " + ", ".join(c["name"] for c in candidates)
        if cdl is not None:
            used.add(id(cdl))
        matches.append(dict(clip, cdl=cdl, status=status))

    unused = [c for c in primary if id(c) not in used] + variants
    return matches, unused


def cdl_to_resolve(cdl, node_index=TARGET_NODE_INDEX):
    return {
        "NodeIndex": str(node_index),
        "Slope": cdl["slope"],
        "Offset": cdl["offset"],
        "Power": cdl["power"],
        "Saturation": cdl["saturation"],
    }


# ---------------------------------------------------------------------------
# Resolve side
# ---------------------------------------------------------------------------

def main():
    resolve = bmd.scriptapp("Resolve")
    fusion = resolve.Fusion()
    ui = fusion.UIManager
    dispatcher = bmd.UIDispatcher(ui)

    existing = ui.FindWindow(win_id)
    if existing:
        existing.Show()
        existing.Raise()
        return

    def get_current_timeline():
        project_now = resolve.GetProjectManager().GetCurrentProject()
        if not project_now:
            return "NO PROJECT"
        timeline_now = project_now.GetCurrentTimeline()
        if not timeline_now:
            return "NO TIMELINE"
        return timeline_now

    # -- Export helpers -----------------------------------------------------

    def export_timeline_ale():
        """Return (ale_text, timeline_name) or (status string, None) on failure."""
        timeline_now = get_current_timeline()
        if isinstance(timeline_now, str):
            return timeline_now, None

        fd, tmp_path = tempfile.mkstemp(suffix=".ale", prefix="cdlmanager_")
        os.close(fd)
        try:
            ok = timeline_now.Export(tmp_path, resolve.EXPORT_ALE_CDL)
            if not ok or not os.path.exists(tmp_path):
                return "EXPORT FAILED", None
            with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(), timeline_now.GetName()
        except Exception as e:
            print(f"[CDLManager] Export error: {e}")
            return "EXPORT FAILED", None
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    # -- Import helpers -----------------------------------------------------

    def clip_key(media_pool_item, item, naming_source):
        if naming_source == "reel":
            return str(media_pool_item.GetClipProperty("Reel Name") or "").strip()
        for prop in ("File Name", "Clip Name"):
            value = str(media_pool_item.GetClipProperty(prop) or "").strip()
            if value:
                return os.path.splitext(value)[0]
        return os.path.splitext(str(item.GetName() or ""))[0]

    def collect_timeline_clips(timeline_now, naming_source):
        clips = []
        for track in range(1, int(timeline_now.GetTrackCount("video")) + 1):
            for item in timeline_now.GetItemListInTrack("video", track) or []:
                media_pool_item = item.GetMediaPoolItem()
                if not media_pool_item:
                    continue  # generators, titles, compound/adjustment without media
                clips.append({
                    "key": clip_key(media_pool_item, item, naming_source),
                    "track": track,
                    "start": int(item.GetStart()),
                    "item": item,
                })
        return clips

    # -- Window -------------------------------------------------------------

    label_w = 110
    button_w = 110
    row_h = 26
    mono = ui.Font({"Family": "Menlo", "PixelSize": 11})

    def preview_row(key, title):
        return ui.HGroup({"Weight": 0}, [
            ui.Label({"Text": title, "MinimumSize": [label_w, 0], "Weight": 0}),
            ui.Label({"ID": f"Preview{key}", "Text": "-", "Weight": 1, "Font": mono}),
        ])

    export_page = ui.VGroup({"Spacing": 8, "Weight": 1}, [
        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Label({"Text": "Naming Source", "MinimumSize": [label_w, row_h], "Weight": 0}),
            ui.ComboBox({"ID": "ExportNaming", "Weight": 1}),
            ui.CheckBox({"ID": "IncludeSop", "Text": "Include SOP Node", "Checked": True, "Weight": 0}),
            ui.CheckBox({"ID": "IncludeSat", "Text": "Include SAT Node", "Checked": True, "Weight": 0}),
        ]),
        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Label({"Text": "Output folder", "MinimumSize": [label_w, row_h], "Weight": 0}),
            ui.LineEdit({"ID": "OutputDir", "PlaceholderText": "Choose a folder…", "Weight": 1}),
            ui.Button({"ID": "ExportBrowse", "Text": "Browse…", "MinimumSize": [button_w, row_h], "Weight": 0}),
        ]),
        ui.Label({
            "Text": "A CDL is generated from the primary grade in the first node of each clip. "
                    "LUT information is included in the description if available.",
            "Weight": 0,
            "WordWrap": True,
        }),
        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Button({"ID": "ExportButton", "Text": "EXPORT CDL", "MinimumSize": [160, row_h + 6], "Weight": 0}),
            ui.Label({"ID": "ProgressText", "Text": "0 / 0", "Weight": 1}),
            ui.Button({"ID": "ResetButton", "Text": "Reset", "MinimumSize": [button_w, row_h], "Weight": 0}),
        ]),
        ui.HGroup({"Weight": 1, "Spacing": 8}, [
            ui.VGroup({"Weight": 1}, [
                ui.Label({"Text": "Generated CDL Files", "Weight": 0}),
                ui.Tree({"ID": "FileTree", "HeaderHidden": True, "Weight": 1}),
            ]),
            ui.VGroup({"Weight": 2}, [
                ui.Label({"Text": "CDL Preview", "Weight": 0}),
                ui.TextEdit({"ID": "PreviewText", "ReadOnly": True, "Font": mono, "Weight": 1,
                             "PlainText": "Select a CDL file to preview"}),
                preview_row("File", "File Name:"),
                preview_row("Slope", "Slope:"),
                preview_row("Offset", "Offset:"),
                preview_row("Power", "Power:"),
                preview_row("Saturation", "Saturation:"),
            ]),
        ]),
    ])

    import_page = ui.VGroup({"Spacing": 8, "Weight": 1}, [
        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Label({"Text": "CDL folder", "MinimumSize": [label_w, row_h], "Weight": 0}),
            ui.LineEdit({"ID": "InputDir", "PlaceholderText": "Folder of .cdl / .cc / .ccc files…", "Weight": 1}),
            ui.Button({"ID": "ImportBrowse", "Text": "Browse…", "MinimumSize": [button_w, row_h], "Weight": 0}),
        ]),
        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Label({"Text": "Match clips by", "MinimumSize": [label_w, row_h], "Weight": 0}),
            ui.ComboBox({"ID": "ImportNaming", "Weight": 1}),
            ui.Label({"Text": "Fallback: first", "Weight": 0}),
            ui.SpinBox({"ID": "MatchChars", "Minimum": 0, "Maximum": 255,
                        "Value": DEFAULT_MATCH_CHARS, "MinimumSize": [70, row_h], "Weight": 0}),
            ui.Label({"Text": "characters (0 = exact only)", "Weight": 0}),
        ]),
        ui.Label({
            "Text": f"CDLs are applied to node {TARGET_NODE_INDEX} of each clip "
                    "(the node Resolve uses for CDL export). Save a timeline copy first: "
                    "undo of scripted changes is not guaranteed.",
            "Weight": 0,
            "WordWrap": True,
        }),
        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Button({"ID": "AnalyseButton", "Text": "ANALYSE", "MinimumSize": [140, row_h + 6], "Weight": 0}),
            ui.Button({"ID": "ApplyButton", "Text": "APPLY CDL", "MinimumSize": [140, row_h + 6], "Weight": 0,
                       "Enabled": False}),
            ui.Label({"ID": "SummaryText", "Text": "", "Weight": 1}),
        ]),
        ui.Tree({"ID": "MatchTree", "Weight": 1, "Font": mono}),
    ])

    window = dispatcher.AddWindow(
        {
            "ID": win_id,
            "WindowTitle": "CDL Manager",
            "Geometry": [200, 140, 980, 700],
            # Modal (not floating/always-on-top): Resolve's main window is blocked
            # until this one is closed, so the timeline can't change mid-operation.
            "WindowModality": "ApplicationModal",
            "WindowFlags": {
                "Window": True,
            },
            "Events": {
                "Close": True,
                "KeyPress": True,
            },
        },
        ui.VGroup({"Spacing": 8, "Weight": 1}, [
            ui.TabBar({"ID": "Tabs", "Weight": 0}),
            ui.Stack({"ID": "Pages", "Weight": 3}, [export_page, import_page]),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Log", "Weight": 1}),
                ui.Button({"ID": "ClearLogButton", "Text": "Clear", "MinimumSize": [button_w, row_h], "Weight": 0}),
            ]),
            ui.TextEdit({"ID": "LogText", "ReadOnly": True, "Font": mono, "Weight": 1}),
        ])
    )

    items = window.GetItems()
    items["Tabs"].AddTab("Export")
    items["Tabs"].AddTab("Import")
    items["Pages"].CurrentIndex = 0
    items["ExportNaming"].AddItems([label for _, label in EXPORT_NAMING_SOURCES])
    items["ImportNaming"].AddItems([label for _, label in IMPORT_NAMING_SOURCES])
    items["FileTree"].ColumnCount = 1

    match_tree = items["MatchTree"]
    match_tree.ColumnCount = 5
    header = match_tree.NewItem()
    for col, title in enumerate(("Track", "Clip", "CDL", "Match", "Result")):
        header.Text[col] = title
    match_tree.SetHeaderItem(header)
    for col, width in enumerate((50, 280, 280, 180, 120)):
        match_tree.ColumnWidth[col] = width

    state = {"files": [], "matches": [], "log_html": []}

    def log(message, is_error=False):
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(f"[CDLManager] {message}")
        safe = xml_escape(line)
        state["log_html"].append(f'<span style="color:#e05252">{safe}</span>' if is_error else safe)
        items["LogText"].HTML = "<br>".join(state["log_html"])
        try:
            bar = items["LogText"].VerticalScrollBar()
            bar.Value = bar.Maximum
        except Exception:
            pass

    def browse_into(field_id, label):
        chosen = fusion.RequestDir(items[field_id].Text or "")
        if chosen:
            items[field_id].Text = str(chosen)
            log(f"{label}: {chosen}")

    # -- Export tab ---------------------------------------------------------

    def set_progress(n, total):
        items["ProgressText"].Text = f"{n} / {total}"

    def reset_preview():
        items["PreviewText"].PlainText = "Select a CDL file to preview"
        for key in ("File", "Slope", "Offset", "Power", "Saturation"):
            items[f"Preview{key}"].Text = "-"

    def show_preview(file_entry):
        items["PreviewText"].PlainText = file_entry["content"]
        items["PreviewFile"].Text = file_entry["name"]
        items["PreviewSlope"].Text = file_entry["data"]["slope"]
        items["PreviewOffset"].Text = file_entry["data"]["offset"]
        items["PreviewPower"].Text = file_entry["data"]["power"]
        items["PreviewSaturation"].Text = file_entry["data"]["saturation"]

    def fill_file_tree():
        tree = items["FileTree"]
        tree.Clear()
        first = None
        for entry in state["files"]:
            tree_item = tree.NewItem()
            tree_item.Text[0] = entry["name"]
            tree.AddTopLevelItem(tree_item)
            if first is None:
                first = tree_item
        if first is not None:
            first.Selected = True
            show_preview(state["files"][0])

    def OnExport(ev=None):
        output_dir = items["OutputDir"].Text.strip()
        if not output_dir:
            log("Error: choose an output folder first", True)
            return "NO OUTPUT DIR"
        if not os.path.isdir(output_dir):
            try:
                os.makedirs(output_dir)
                log(f"Created folder {output_dir}")
            except Exception as e:
                log(f"Error: cannot create {output_dir}: {e}", True)
                return "NO OUTPUT DIR"

        state["files"] = []
        items["FileTree"].Clear()
        reset_preview()
        set_progress(0, 0)

        ale_text, timeline_name = export_timeline_ale()
        if timeline_name is None:
            log(f"Error: {ale_text}", True)
            return ale_text
        log(f"Exported timeline '{timeline_name}' to ALE (with CDL)")

        columns, data_lines, message = parse_ale(ale_text)
        log(message, columns is None)
        if columns is None:
            return "PARSE FAILED"

        naming_source = EXPORT_NAMING_SOURCES[int(items["ExportNaming"].CurrentIndex)][0]
        clips = extract_clips(columns, data_lines, naming_source, log)
        if not clips:
            log("Error: No valid clip data found in ALE file", True)
            return "NO CLIPS"

        clips = dedupe_clips(clips, log)
        log(f"Found {len(clips)} clips with CDL data")

        state["files"] = write_cdl_files(
            clips,
            output_dir,
            bool(items["IncludeSop"].Checked),
            bool(items["IncludeSat"].Checked),
            log,
            set_progress,
        )
        fill_file_tree()
        log(f"Export complete! {len(state['files'])} CDL files in {output_dir}")
        return "OK"

    def OnReset(ev=None):
        items["ExportNaming"].CurrentIndex = 0
        items["IncludeSop"].Checked = True
        items["IncludeSat"].Checked = True
        items["FileTree"].Clear()
        state["files"] = []
        set_progress(0, 0)
        reset_preview()
        log("Reset export fields")

    def OnFileClicked(ev):
        name = ev["item"].Text[0]
        for entry in state["files"]:
            if entry["name"] == name:
                show_preview(entry)
                return

    # -- Import tab ---------------------------------------------------------

    def fill_match_tree(results=None):
        match_tree.Clear()
        for n, m in enumerate(state["matches"]):
            row = match_tree.NewItem()
            row.Text[0] = str(m["track"])
            row.Text[1] = m["key"] or "(no name)"
            row.Text[2] = m["cdl"]["file"] if m["cdl"] else "-"
            row.Text[3] = m["status"]
            row.Text[4] = results[n] if results else ""
            match_tree.AddTopLevelItem(row)

    def OnAnalyse(ev=None):
        items["ApplyButton"].Enabled = False
        state["matches"] = []
        match_tree.Clear()

        folder = items["InputDir"].Text.strip()
        if not folder or not os.path.isdir(folder):
            log("Error: choose an existing CDL folder first", True)
            return "NO INPUT DIR"

        timeline_now = get_current_timeline()
        if isinstance(timeline_now, str):
            log(f"Error: {timeline_now}", True)
            return timeline_now

        cdls = load_cdl_folder(folder, log)
        log(f"Read {len(cdls)} CDLs from {folder}")
        if not cdls:
            return "NO CDL"

        naming_source = IMPORT_NAMING_SOURCES[int(items["ImportNaming"].CurrentIndex)][0]
        clips = collect_timeline_clips(timeline_now, naming_source)
        clips.sort(key=lambda c: (c["track"], c["start"]))
        log(f"Timeline '{timeline_now.GetName()}': {len(clips)} clips")

        matches, unused = match_clips(clips, cdls, int(items["MatchChars"].Value))
        state["matches"] = matches
        fill_match_tree()

        matched = sum(1 for m in matches if m["cdl"])
        for m in matches:
            if m["status"].startswith("ambiguous"):
                log(f"Warning: {m['key']} is {m['status']}", True)
        for c in unused:
            log(f"Warning: CDL not used: {c['name']} ({c['file']})", True)
        items["SummaryText"].Text = (
            f"{matched} / {len(matches)} clips matched, {len(unused)} CDL unused"
        )
        items["ApplyButton"].Enabled = matched > 0
        return "OK"

    def node_count(item):
        try:
            graph = item.GetNodeGraph()  # Resolve 19+
            if graph:
                return int(graph.GetNumNodes())
        except Exception:
            pass
        try:
            return int(item.GetNumNodes())  # older API
        except Exception:
            return None

    def OnApply(ev=None):
        # While this ApplicationModal window is shown, Resolve doesn't see its main
        # window (GetCurrentPage() returns None) and SetCDL returns False on every
        # clip. The same call works from the Console on the Color page, so: hide the
        # window, switch to Color, apply, then show the window again.
        window.Hide()
        results = []
        applied = 0
        try:
            if resolve.GetCurrentPage() != "color":
                resolve.OpenPage("color")
            log(f"Resolve {resolve.GetVersionString()}, page '{resolve.GetCurrentPage()}'")

            for m in state["matches"]:
                if not m["cdl"]:
                    results.append("")
                    continue
                try:
                    ok = m["item"].SetCDL(cdl_to_resolve(m["cdl"]))
                except Exception as e:
                    print(f"[CDLManager] SetCDL error on {m['key']}: {e}")
                    ok = False
                if ok:
                    applied += 1
                    results.append("applied")
                else:
                    results.append("FAILED")
                    log(f"Error: SetCDL failed on {m['key']} (track {m['track']}): "
                        f"nodes={node_count(m['item'])}, sent={cdl_to_resolve(m['cdl'])}", True)
        finally:
            window.Show()
            window.Raise()

        fill_match_tree(results)
        items["ApplyButton"].Enabled = False
        log(f"Applied {applied} CDLs to node {TARGET_NODE_INDEX}")
        items["SummaryText"].Text = f"{applied} CDL applied"
        return "OK"

    def OnImportSettingsChanged(ev=None):
        # Matches are stale once the folder or matching rules change.
        items["ApplyButton"].Enabled = False

    # -- Shared -------------------------------------------------------------

    def OnTabChanged(ev):
        items["Pages"].CurrentIndex = ev["Index"]

    def OnClearLog(ev=None):
        state["log_html"] = []
        items["LogText"].HTML = ""

    # Same Ctrl/Cmd swap as QCHelper: physical Control reports as MetaModifier.
    CLOSE_SHORTCUT_KEY = ord("W")
    CTRL_MODIFIER_INT_MASK = 0x10000000
    CTRL_MODIFIER_NAME_HINTS = ("meta",)

    def has_ctrl(ev):
        modifiers = ev.get("Modifiers", None)
        if isinstance(modifiers, dict):
            return any(pressed and any(h in str(name).lower() for h in CTRL_MODIFIER_NAME_HINTS)
                       for name, pressed in modifiers.items())
        if isinstance(modifiers, int):
            return bool(modifiers & CTRL_MODIFIER_INT_MASK)
        return False

    def OnWindowKeyPress(ev):
        key = ev["Key"]
        if key == 16777216:  # Escape
            dispatcher.ExitLoop()
            return
        if key == CLOSE_SHORTCUT_KEY and has_ctrl(ev):
            dispatcher.ExitLoop()

    def OnClose(ev):
        dispatcher.ExitLoop()

    window.On[win_id].KeyPress = OnWindowKeyPress
    window.On[win_id].Close = OnClose
    window.On["Tabs"].CurrentChanged = OnTabChanged
    window.On["ClearLogButton"].Clicked = OnClearLog

    window.On["ExportBrowse"].Clicked = lambda ev: browse_into("OutputDir", "Output folder")
    window.On["ExportButton"].Clicked = OnExport
    window.On["ResetButton"].Clicked = OnReset
    window.On["FileTree"].ItemClicked = OnFileClicked

    window.On["ImportBrowse"].Clicked = lambda ev: browse_into("InputDir", "CDL folder")
    window.On["AnalyseButton"].Clicked = OnAnalyse
    window.On["ApplyButton"].Clicked = OnApply
    window.On["InputDir"].TextChanged = OnImportSettingsChanged
    window.On["ImportNaming"].CurrentIndexChanged = OnImportSettingsChanged
    window.On["MatchChars"].ValueChanged = OnImportSettingsChanged

    window.Show()
    log("Ready. Export: choose an output folder, then EXPORT CDL. "
        "Import: choose a CDL folder, ANALYSE, then APPLY CDL.")
    dispatcher.RunLoop()
    window.Hide()


if "bmd" in globals():
    main()
