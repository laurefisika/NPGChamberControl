# Historical bug record

This is a concise, English version of the working bug register. It records what was observed and how the final package addressed it.

| Date | ID | Area | Observation | Final disposition |
| --- | --- | --- | --- | --- |
| 2026-06-03 | 001 | Phase 01 | XGS600 pressure acquisition failed with a Windows `PermissionError(13)` / `WriteFile failed` serial error. | Replaced restart-only recovery with explicit serial ownership cleanup and launcher-side port verification. |
| 2026-06-04 | 002 | Phase 02 | An unavailable pressure value was represented as `NaN` and incorrectly caused the sputtering workflow to enter an error state. | `NaN` remains visible and logged but is handled as unavailable data rather than a false pressure violation. |
| 2026-06-05 | 003 | Phase 03 | Oven PID port `COM9` could not be opened after earlier phases because the handle remained busy. | Added pre- and post-phase checks, buffer clearing, retries, and blocking of the next phase until every chamber port is released. |
| 2026-06-05 | 004 | Phase 03 | The Keysight current was capped by an inappropriate limit for the intended workflow. | The current-limit relationship was reviewed and the final behavior was aligned with the phase-specific recipe and safety logic recorded in the changelog. |

The raw Word register is retained outside this future-public repository candidate because it is an internal working document. This Markdown summary is the canonical GitHub record.

