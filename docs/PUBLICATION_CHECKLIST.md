# Publication checklist

The private technical archive and the public portfolio repository should be separate Git repositories. Deleting a sensitive file in a later commit does not remove it from earlier Git history.

Before creating a public export:

- [ ] Obtain written confirmation that the code may be published and clarify whether ICN2, the research group, a supervisor, or another contributor owns any part of it.
- [x] Record the author's professional identity: Laura Rodríguez Jordán, GitHub `laurefisika`, contact `laurarodriguezjordan2.0@gmail.com`.
- [ ] Confirm contributor credits and the exact affiliation wording with the institution.
- [ ] Select a license only after ownership is clear. Do not replace the current internal-use notice with MIT, GPL, Apache, or another open-source license without authorization.
- [ ] Remove the laboratory COSCON IP address and use a documented local configuration or environment variable.
- [ ] Exclude `history/2026-07-recovered-packages/archives/` and the historical COSCON diagnostics from the public export; the private source-backed files intentionally retain laboratory-local network settings.
- [ ] Exclude manufacturer manuals, service guides, vendor software guides, equipment passwords, and any copied material that cannot legally be redistributed.
- [ ] Review the phase explanation PDFs and screenshots for internal IPs, sample names, computer names, room information, and unpublished results.
- [ ] Review the historical notebook output before publication; clear or replace experimental output if the results are not approved for release.
- [ ] Confirm that no `Data Samples`, logs, databases, saved modes, local settings, or credentials are tracked.
- [ ] Describe validation accurately and keep incomplete hardware acceptance items explicit.
- [x] Add the author profile, contact route and citation metadata.
- [ ] Add the final public repository topics and approved screenshots.
- [ ] Create a fresh public repository from the reviewed export so the private archive's history is never exposed.

Suggested public repository name: `npg-chamber-automation`.

Suggested topics: `python`, `laboratory-automation`, `instrument-control`, `ultra-high-vacuum`, `nanoporous-graphene`, `serial-communication`, `udp`, `scientific-software`.
