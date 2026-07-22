# Recovered July project packages

This directory preserves the intermediate NPG Chamber packages that were shared through the project conversations in July 2026. Every entry is backed by a real ZIP archive; no source state was reconstructed from prose.

## How to use this archive

- `browsable-source/` exposes the four phase scripts and the launcher, configuration, device and handoff modules most relevant to each revision. Use this area to read and compare the evolution directly on GitHub.
- `archives/` contains the complete original ZIPs, including tests, support modules, documentation and source manifests that are not repeated in the browsable view.
- `SOURCE_ARCHIVES.sha256` records the SHA-256 digest of every recovered ZIP.
- The supported runtime remains the project at the repository root. Nothing under `history/` is launched by `npg-chamber`.

The historical files are intentionally preserved as they were supplied. Some contain laboratory-local COM settings and the private COSCON network address, so this archive must remain private and must not be copied into a future public portfolio repository.

## Chronological index

| Date and time | Internal version | Browsable snapshot | Original archive | Main change represented |
|---|---:|---|---|---|
| 13 Jul 08:20 | 0.9.11 | [`automation-parameters`](browsable-source/2026-07-13_0.9.11_automation-parameters/) | `npg_chamber_project_v4_automation_parameters_v0.9.11.zip` | Run-only **Change automatization parameters** editor |
| 13 Jul 08:58 | 0.9.12 | [`serial-handoff`](browsable-source/2026-07-13_0.9.12_serial-handoff/) | `npg_chamber_project_v4_automation_parameters_serial_handoff_v0.9.12.zip` | Active COM-port release and handoff checks |
| 13 Jul 09:18 | 0.9.12 | [`backups-restored`](browsable-source/2026-07-13_0.9.12_backups-restored/) | `npg_chamber_project_v4_automation_parameters_serial_handoff_v0.9.12(1).zip` | Corrected archive with the original-script backups retained |
| 14 Jul 14:59 | 0.9.12 | [`COSCON read-only diagnostic`](browsable-source/2026-07-14_0.9.12_coscon-read-only-diagnostic/) | `npg_chamber_project_v6_COSCON_read_only_diagnostic.zip` | Read-only COSCON UDP diagnostic added |
| 15 Jul 11:50 | 0.10.0 | [`Phase 02 prototype`](browsable-source/2026-07-15_0.10.0_phase2-automation-prototype/) | `npg_chamber_project_v6_phase2_automated_v0.10.0.zip` | Temporary direct-COSCON automation prototype |
| 15 Jul 13:18 | 0.9.13 | [`Phase 02 automation`](browsable-source/2026-07-15_0.9.13_phase2-automation/) | `npg_chamber_project_v6_phase2_automated_v0.9.13.zip` | Direct COSCON control integrated under the retained 0.9.x version line |
| 15 Jul 14:04 | 0.9.14 | [`Phase 02 dashboard`](browsable-source/2026-07-15_0.9.14_phase2-dashboard/) | `npg_chamber_project_v6_phase2_dashboard_v0.9.14.zip` | UDP framing fix and operator dashboard |
| 15 Jul 14:21 | 0.9.15 | [`0.9.15 dashboard`](browsable-source/2026-07-15_0.9.15_phase2-dashboard/) | `npg_chamber_project_v6_phase2_dashboard_v0.9.15.zip` | Compact, readable Phase 02 layout and honest Degas timing |
| 15 Jul 14:31 | 0.9.15 | [`editable targets`](browsable-source/2026-07-15_0.9.15_editable-targets/) | `npg_chamber_project_v6_phase2_dashboard_v0.9.15_targets_editable.zip` | COSCON energy and emission targets exposed as run parameters |
| 15 Jul 15:09 | 0.9.15 | [`readable layout`](browsable-source/2026-07-15_0.9.15_readable-layout/) | `npg_chamber_project_v6_phase2_dashboard_v0.9.15_readable_layout.zip` | Larger text and reorganized three-column dashboard |
| 21 Jul 10:50 | 0.9.16 | [`pyrometer integration`](browsable-source/2026-07-21_0.9.16_pyrometer-integration/) | `npg_chamber_project_v9.zip` | IMPAC IPE 140 profiles and Phase 01/03 temperature views |
| 21 Jul 11:40 | 0.9.17 | [`initial 0.9.17`](browsable-source/2026-07-21_0.9.17_initial/) | `npg_chamber_project_v10.zip` | First 0.9.17 integrated state |
| 21 Jul 13:55 | 0.9.17 | [`Phase 01/03 refinement`](browsable-source/2026-07-21_0.9.17_phase13-refinement/) | `npg_chamber_project_v10_refined.zip` | Phase 01/03 visual and panel refinement |
| 21 Jul 14:18 | 0.9.17 | [`saved modes`](browsable-source/2026-07-21_0.9.17_saved-modes/) | `npg_chamber_project_v10_modes_refined.zip` | Persistent complete-chamber automation modes |
| 21 Jul 14:54 | 0.9.17 | [`pre-final`](browsable-source/2026-07-21_0.9.17_pre-final/) | `npg_chamber_project_v12.zip` | Later pre-final package |
| 21 Jul 15:36 | 0.9.17 | [`pre-final updated`](browsable-source/2026-07-21_0.9.17_pre-final-updated/) | `npg_chamber_project_v12_updated.zip` | Last recovered 0.9.17 package before the 0.9.18 release |

The temporary `0.10.0` package is retained because it is a genuine source-backed state. It was superseded the same day by `0.9.13`; it is not presented as the start of a separate supported version line.

