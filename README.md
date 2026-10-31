# Resolve CDL Manager

DaVinci Resolve (Workflow Integration) plugin for **exporting** and **importing** ASC CDLs
(v1.01), one `.cdl` file per clip. A single window with two tabs, **Export** and
**Import**, and a shared log.

The export logic is modelled on the "ALE to CDL Converter" page
(https://240p-is-fine.com/), but runs directly inside Resolve.

Example of a generated file:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<ColorDecisionList xmlns="urn:ASC:CDL:v1.01">
	<ColorDecision>
		<ColorCorrection>
			<SOPNode>
				<Description>A_0018C003_260922_192934_h1CHU - LogC4_to_LogC4_K1S1_ld.cube</Description>
				<Slope>0.7357 0.7370 0.7297</Slope>
				<Offset>0.0386 0.0389 0.0390</Offset>
				<Power>1.0200 1.0200 1.0200</Power>
			</SOPNode>
			<SATNode>
				<Saturation>1.0000</Saturation>
			</SATNode>
		</ColorCorrection>
	</ColorDecision>
</ColorDecisionList>
```

Both directions work on **node 1**: Resolve itself states "A CDL is generated using the
primary grade in the first node of each clip".

## Export tab

1. Open the timeline, choose an **Output folder** (`Browse…`).
2. Set the options, then click **EXPORT CDL**.
3. Click a file in the list to preview it.

Options and behaviour:
- **Naming Source**: `Auto (Name → Reel)`, `Clip Name Only` (the extension is removed) or
  `Reel Name Only`.
- **Include SOP Node** / **Include SAT Node**.
- **Description**: `<clip name> - <LUT>` when the timeline has a `LUT Used` / `LUT Nodes`
  value, otherwise just the clip name.
- **Duplicates**: a clip used several times with the same values gives a single file. If
  the values differ, the files are `name.cdl`, `name_2.cdl`, `name_3.cdl`… in timeline
  order, and a warning is logged.
- Existing files are overwritten, and this is shown in the log.

## Import tab

1. **CDL folder**: a folder of `.cdl`, `.cc` or `.ccc` files. A `.ccc` holding several
   CDLs uses the `id` of each one.
2. **Match clips by**: `Clip Name` (the media file name without extension) or `Reel Name`.
3. **Fallback: first N characters** (10 by default). When no file has exactly the clip's
   name, the first N characters are compared. Example: `A_0018C003_260922_192934_h1CHU`
   (clip) ↔ `A_0018C003_260922_192934_a1CHU.cdl` (file). `0` = exact match only.
4. **ANALYSE**: shows a table Track / Clip / CDL / Match type, with no change to the
   timeline. The log lists ambiguous matches (several files share the same first N
   characters: nothing is applied) and CDL files that were not used.
5. **APPLY CDL**: applies `SetCDL` on node 1, with a result per clip. The button turns off
   again as soon as the folder or matching settings change, so you have to re-analyse.

Notes:
- A clip used several times gets the same CDL each time. The `name_2`, `name_3` variants
  created by the export are listed as "not used" and are not applied automatically.
- A missing SOP or SAT node is replaced by neutral values (1 / 0 / 1, saturation 1).
- **Save a copy of the timeline first**: undo of changes made by a script is not
  guaranteed.

## Window

- **Modal**: Resolve can't be used while the window is open.
- `Escape` or `Ctrl+W` closes it.

## Installation

1. Copy `CDLManager.py` into:

   ```
   /Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins
   ```

2. Restart DaVinci Resolve if it was open.
3. Launch it from **Workspace > Workflow Integrations > CDLManager**.

## Known limitations

- **Export at 4-decimal precision**: the values come from the ALE that Resolve generates
  (`EXPORT_ALE_CDL`), which writes `ASC_SOP` to 4 decimals. Import, on the other hand,
  passes the files' values through as they are.
- The Resolve API has no `GetCDL()`. The export goes through a temporary ALE export of the
  timeline, which is deleted afterwards.
- Anything that isn't CDL (curves, qualifiers, other nodes) is neither exported nor
  imported.

## Development

See `CLAUDE.md`.
