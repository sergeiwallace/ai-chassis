---
title: Collision-Resistant, Human-Readable Issue-ID Schemes
category: research
tags: [research, beads, display-id, identifiers, distributed-systems, ux]
status: complete
source: "GPT-5 Codex, primary-source research verified 2026-08-09"
template_version: "research-1.2.0"
---
<!-- Canonical Jinja source: STUB.md.jinja. This direct-copy stub is retained for consumers that have not migrated to rendering it. -->

# Collision-Resistant, Human-Readable Issue-ID Schemes

**Status:** complete

**Created:** 2026-08-09

<!-- doc:region name="context" kind="immutable" -->

## Table of Contents

- [Context](#context)
- [Executive Summary](#executive-summary)
- [1. Identifier Families Used by Distributed Systems](#1-identifier-families-used-by-distributed-systems)
- [2. Human-Memorable Naming Without Central Coordination](#2-human-memorable-naming-without-central-coordination)
- [3. Word-Slug Collision Math and Curation](#3-word-slug-collision-math-and-curation)
- [4. Migration Without Citation Breakage](#4-migration-without-citation-breakage)
- [5. Length Versus Human Usability](#5-length-versus-human-usability)
- [6. Content- and Title-Derived Slugs](#6-content--and-title-derived-slugs)
- [7. Issue-Tracker and DVCS-Adjacent Prior Art](#7-issue-tracker-and-dvcs-adjacent-prior-art)
- [8. Gaps, Blindspots & Emergent Findings](#8-gaps-blindspots--emergent-findings)
- [9. Comparison](#9-comparison)
- [10. Recommendation](#10-recommendation)
- [11. Open Questions](#11-open-questions)
- [Sources](#sources)
- [Ambiguous Items from Auto-Remediation (Post-Run Review)](#ambiguous-items-from-auto-remediation-post-run-review)
- [Appendix: Research Prompt](#appendix-research-prompt)
- [Appendix: Provenance Ledger](#appendix-provenance-ledger)
- [Run History](#run-history)

## Context

Feeds `ISSUE-REF` (hash `ISSUE-REF`, "Replace the sequential PREFIX-N display id with a scheme
that cannot collide across machines") — a design task already fully scoped in Beads, including
the structural root cause (a per-machine `max(N)+1` allocator against a fleet-wide-unique
namespace, unfixable by locking alone) and five candidate directions to evaluate. This research
survey exists to ground the design doc's recommendation in how other real systems have solved
the same problem — offline-mintable, collision-resistant, human-memorable identifiers — rather
than inventing a scheme from first principles.

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->

## Executive Summary

1. The word “never” in the offline uniqueness requirement changes the answer. A finite random identifier space—including UUIDv4, UUIDv7, ULID, NanoID, KSUID, random words, and content hashes modeled as random—can make collision probability negligible but cannot make it zero. Disjoint, permanently assigned node namespaces plus non-repeating local state can provide a conditional guarantee without a mint-time round trip; random-only schemes cannot. The distinction follows from the pigeonhole principle and from the collision models specified for UUIDs. [INFERENCE]

2. The strongest fit is a hardened actor-token plus durable node-global sequence, rendered with a deterministic check word, for example `ISSUE-REF`. The identity-bearing tuple is `(fleet node token, uint64 sequence)`; the project prefix is routing context and the final word detects common transcription errors. This is the same architectural family as Snowflake's worker-plus-sequence partitioning and Automerge's actor-plus-counter operation IDs, without depending on wall-clock ordering. [INFERENCE]

3. A memorable random word slug is much longer than `ISSUE-REF` once the collision target is stated. At 100,000 issues, two words from either a 2,048-word or 7,776-word list collide with near certainty; reaching a birthday-bound probability below \(10^{-12}\) requires at least 73 random bits—seven 2,048-way words or six 7,776-way words. [INFERENCE]

4. Adoption should be a writer cutover, not a citation flag day: freeze the sequential allocator; preserve every existing `PREFIX-N` identifier as a permanent alias to its canonical record; resolve both grammars indefinitely; and mint only the new grammar after cutover. [INFERENCE] Jira, GitHub, and Git's SHA transition designs preserve old references through historical-key resolution, redirects, or translation mappings rather than renumbering published objects. [VERIFIABLE][^1][^2][^3]

5. Word encodings can aid speech, but controlled studies find longer entry times or more typing errors for word-based secrets and mixed performance for spoken versus visual fingerprint comparison. [VERIFIABLE][^4][^5][^6] Direct issue-ID-specific HCI evidence remains absent. [NO SOURCE] A compact structural ID plus checksum/check word, completion, tolerant parsing, and title-first lookup is therefore a better-supported design bet than either a six-word canonical slug or an opaque hash alone. [INFERENCE]

## 1. Identifier Families Used by Distributed Systems

### 1.1 Two fundamentally different guarantees

There are two families that are often described with the same word, “unique”:

- **Probabilistic uniqueness:** draw from a large space. Under independent uniform sampling, collision probability is tiny but nonzero.
- **Partitioned uniqueness:** assign each writer a disjoint namespace and let it allocate a non-repeating local value. Collision probability is zero only while the namespace and local-state invariants hold.

RFC 9562 explicitly treats collision resistance as a property that depends on the UUID version's generation algorithm and advises distributed applications to consider collision probability and synchronization. UUIDv4 has 122 random bits; UUIDv7 combines a 48-bit Unix-millisecond timestamp with 74 remaining bits available for randomness and/or monotonic sequencing. [VERIFIABLE][^7]

For independent uniform draws from a space of size \(M\), the birthday approximation used throughout this report is:

\[
P(\text{at least one collision})
= 1-\prod_{i=0}^{n-1}\left(1-\frac{i}{M}\right)
\approx 1-e^{-n(n-1)/(2M)}.
\]

The calculations below assume honest independent uniform entropy. They do **not** cover a broken random-number generator, copied VM entropy/state, adversarial choices, or the structured invariants of worker-and-counter schemes. [HEURISTIC]

### 1.2 Survey

| Scheme | Effective uniqueness mechanism | Approximate collision probability at 100,000 IDs | Coordination / clock assumptions | Ordering |
|---|---|---:|---|---|
| UUIDv4 | 122 random bits | \(9.4\times10^{-28}\) | No coordination or clock; requires adequate independent randomness | None |
| UUIDv6 | Reordered v1 timestamp, clock sequence, and node fields | Not a single birthday space | Unique node/clock-sequence behavior and clock-state handling | Time-oriented under its clock assumptions |
| UUIDv7 | 48-bit millisecond time plus 74 random/monotonic bits | Worst case, all in one millisecond: \(2.65\times10^{-13}\) if all 74 bits are random | Clock plus randomness; monotonic implementations retain per-process state | Broad timestamp order, not a global total order |
| ULID | 48-bit millisecond time plus 80 random bits | Worst case, one millisecond: \(4.14\times10^{-15}\) | Clock and randomness; monotonic factory increments within one generator | Lexicographic by timestamp; same-millisecond behavior depends on generator |
| KSUID | 32-bit second timestamp plus 128 random bits | Worst case, one second: \(1.47\times10^{-29}\) | Clock and randomness | Rough chronological order |
| Snowflake | Timestamp + assigned worker/datacenter + per-millisecond sequence | Zero only if worker IDs do not overlap, sequence does not wrap, and clocks do not regress | Worker provisioning, durable/runtime state, clock discipline | Time-oriented under clock assumptions |
| Sonyflake | 10 ms time + sequence + machine ID | Same conditional model as Snowflake | Unique machine ID and clock discipline; default machine ID is derived from private IP | Time-oriented under clock assumptions |
| Instagram variant | Time + logical shard + per-second sequence | Zero only while shard/sequence invariants hold | Preassigned shard and database-local allocation | Time-oriented under clock assumptions |
| NanoID default | 21 symbols from a 64-symbol alphabet = 126 random bits | \(5.88\times10^{-29}\) | No coordination or clock; secure randomness | None |
| Cuid2 default | Hash of mixed random, time, counter, and host/session inputs | Project claims a very large collision space; no simple independent-bit proof | Local entropy and non-pathological process state | Intentionally not chronological |
| Git SHA-256 object ID | Hash of object type, length, and content | Random-oracle approximation: \(4.32\times10^{-68}\) | No clock or coordination; identical canonical content deliberately has identical ID | None |

The numeric probabilities are calculations from the birthday formula under the stated independent-uniform model. [INFERENCE] The field layouts and stated algorithms come from their specifications or original implementations. [VERIFIABLE][^7][^8][^9][^10][^11][^12][^13][^14][^15][^16]

### 1.3 Practical interpretation

UUIDv4, NanoID, and KSUID are excellent operational identifiers when “negligible” is acceptable. Their remaining collision risks at this project's scale are dwarfed by storage corruption, implementation bugs, and accidental state cloning. They nevertheless fail a literal zero-collision reading, and their canonical renderings are not especially speakable. [INFERENCE]

Cuid2's project documentation gives a default length of 24 and advertises roughly \(4.026\times10^{18}\) generated IDs at a 50% collision chance. [VERIFIABLE][^14] Because its construction hashes mixed random, time, counter, and host/session inputs, this report treats that number as the project's claim rather than as an independently proven uniform birthday space. [INFERENCE]

UUIDv7 and ULID place timestamps first to support time-oriented sorting, and ULID's monotonic mode increments its random field within one generator. [VERIFIABLE][^7][^8] Time does not add independent entropy within one bucket, so the random portion still carries the birthday bound; separate machines remain uncoordinated, and clock rollback or skew prevents authoritative fleet chronology. [INFERENCE]

Snowflake-family schemes obtain exactness by partitioning the output space rather than through the timestamp alone. [INFERENCE] Twitter's original implementation dedicates bits to datacenter and worker IDs, rejects backward clock movement, and waits when its per-millisecond sequence is exhausted. Sonyflake likewise requires a machine ID and warns that its default private-IP-derived value may need replacement; Instagram assigned logical shard IDs and allocated the sequence inside PostgreSQL. [VERIFIABLE][^10][^11][^12]

Those schemes are offline at **mint time**, but a new writer cannot safely invent its own worker ID. The fleet must provision a never-reused namespace beforehand. That is compatible with the stated constraint if provisioning is treated as machine enrollment rather than issue creation; it is not compatible if every arbitrary clone must begin minting with no prior identity. [INFERENCE]

Git demonstrates the ergonomic opposite. Content addressing gives an immutable object's full hash a stable identity, and Git's SHA-256 transition design explicitly keeps SHA-1/SHA-256 mappings and supports both object-name formats during migration. [VERIFIABLE][^3][^16] For mutable issue records, an ID derived from the current title or body would change on edit, so a content hash must instead identify an immutable creation object or a stable canonical object lineage. [INFERENCE]

## 2. Human-Memorable Naming Without Central Coordination

### 2.1 Friendly names are usually aliases, not global identities

Docker automatically generates easy-to-remember names such as `vibrant_cannon`, while separately assigning a random container ID. [VERIFIABLE][^17] Because the daemon assigns and resolves the friendly name, this is not evidence of a fleet-wide collision-proof identity scheme. [INFERENCE]

Heroku generates an app name when one is omitted, app names live in a platform namespace, current Common Runtime hostnames append a random identifier to the app name, and renaming changes the old hostname. [VERIFIABLE][^18] This is evidence for “friendly label plus strong suffix,” not for decentralized word-pair allocation. [INFERENCE]

what3words assigns a predetermined three-word address to each fixed geographic square and provides offline SDK lookup plus spelling/order correction. [VERIFIABLE][^19][^20] Its uniqueness therefore comes from a predefined global mapping over a finite domain, not from independent random three-word generation; geographic context also supplies information unavailable to arbitrary issue IDs. [INFERENCE]

The recurring design pattern is separation: a hard identity carries uniqueness; a friendly name helps recall and discovery. Treating the friendly layer as the sole identity imports collision and rename behavior that these systems avoid through a daemon, registry, predetermined mapping, or underlying opaque ID. [INFERENCE]

### 2.2 Word encodings as entropy and error-detection codes

EFF's long Diceware list has 7,776 entries so each word corresponds to one of five dice; its curation removed offensive or emotionally charged terms, difficult spellings, homophones, and confusing prefixes. EFF also notes that the list's usability choices were not backed by a formal usability study. [VERIFIABLE][^21]

S/KEY encodes a 64-bit value plus checksum in six words selected from a 2,048-word dictionary. RFC 1751's earlier convention encodes 128-bit keys in twelve short English words and explicitly identifies language dependence as an interoperability disadvantage. The PGP Word List uses alternating 256-word vocabularies selected for phonetic distinctiveness so that substitutions, omissions, duplications, and transpositions are easier to notice. [VERIFIABLE][^22][^23][^24]

These are valuable precedents for rendering bytes for speech. They also reveal the unavoidable length: a 2,048-word token contributes only 11 bits, and an error-checking word contributes detection rather than uniqueness. A two-word slug from such a list carries 22 bits, not “hash-like” collision resistance. [INFERENCE]

### 2.3 Prior art specifically for memorable issue IDs

No current primary source was found for a production issue tracker whose **canonical** IDs are short random word combinations, globally collision-proof, and minted by arbitrary uncoordinated offline clients. Searches across distributed trackers, friendly-ID libraries, DVCS issue tools, and 2024–2026 literature instead found canonical hashes, UUIDs, or actor/counter structures with friendly search and abbreviation layers. [NO SOURCE]

This negative result should not be read as proof that no such system exists. It means the proposed two-word canonical slug lacks demonstrated issue-tracker precedent in the primary sources located for this run. [NO SOURCE]

## 3. Word-Slug Collision Math and Curation

### 3.1 Birthday bounds

Let \(W\) be the number of admissible words and \(k\) the number of independently sampled words. The namespace is \(M=W^k\); for typed lists such as adjective+noun, use \(M=\prod_j W_j\), so the space is the product of the list sizes rather than the sum. Applying the birthday formula gives: [INFERENCE]

| Wordlist and words | Entropy | \(P\) at 1,000 issues | \(P\) at 10,000 | \(P\) at 100,000 |
|---|---:|---:|---:|---:|
| 2,048 × 2 | 22 bits | 11.2% | 99.999% | ~100% |
| 2,048 × 3 | 33 bits | \(5.81\times10^{-5}\) | 0.580% | 44.1% |
| 2,048 × 4 | 44 bits | \(2.84\times10^{-8}\) | \(2.84\times10^{-6}\) | \(2.84\times10^{-4}\) |
| 2,048 × 5 | 55 bits | \(1.39\times10^{-11}\) | \(1.39\times10^{-9}\) | \(1.39\times10^{-7}\) |
| 2,048 × 6 | 66 bits | \(6.77\times10^{-15}\) | \(6.78\times10^{-13}\) | \(6.78\times10^{-11}\) |
| 2,048 × 7 | 77 bits | \(3.31\times10^{-18}\) | \(3.31\times10^{-16}\) | \(3.31\times10^{-14}\) |
| 7,776 × 3 | 38.8 bits | \(1.06\times10^{-6}\) | \(1.06\times10^{-4}\) | 1.06% |
| 7,776 × 4 | 51.7 bits | \(1.37\times10^{-10}\) | \(1.37\times10^{-8}\) | \(1.37\times10^{-6}\) |
| 7,776 × 5 | 64.6 bits | \(1.75\times10^{-14}\) | \(1.76\times10^{-12}\) | \(1.76\times10^{-10}\) |
| 7,776 × 6 | 77.6 bits | \(2.26\times10^{-18}\) | \(2.26\times10^{-16}\) | \(2.26\times10^{-14}\) |

These are computed values, rounded upward where presentation precision would otherwise imply false exactness. [INFERENCE]

At \(n=100{,}000\), the approximate entropy requirements are 53 bits for \(P\le10^{-6}\), 63 bits for \(P\le10^{-9}\), 73 bits for \(P\le10^{-12}\), and 83 bits for \(P\le10^{-15}\). Therefore a practical random-word canonical ID needs six or seven words for a conservative “negligible” target—and still does not satisfy literal “never.” [INFERENCE]

Adding a database collision check does not repair the offline case: during a partition, two clients can mint the same slug before either sees the other. Retrying after merge violates stability once either ID has been cited. [INFERENCE]

### 3.2 Curation hazards

A production wordlist needs more than profanity filtering:

- Remove homophones, near-homophones, plural/singular pairs, ambiguous compounds, and words whose prefixes become indistinguishable over noisy audio. [HEURISTIC]
- Bound token length; otherwise the worst-case branch or filename is far longer than the average. EFF's long list accepts words of three to nine characters and reports an average near seven, illustrating the spread. [VERIFIABLE][^21]
- Define one canonical ASCII lowercase form and separator. Case-insensitive filesystems and URL normalization make case a poor entropy source. [HEURISTIC]
- Test with non-native English speakers and speech-to-text. RFC 1751 explicitly calls a single-language list a disadvantage, while EFF says more usability research is needed. [VERIFIABLE][^21][^23]
- Treat the list as versioned protocol data. Removing a word later must not invalidate an existing ID, and different clients must not silently sample different universes. [INFERENCE]
- Exclude sensitive or alarming terms because IDs propagate into branches, commit subjects, alerts, and public URLs. [HEURISTIC]

A check word can improve human distinguishability, but it must be derived deterministically from the identity-bearing fields and excluded from uniqueness calculations. Otherwise implementations may wrongly assume that a typo detector adds independent entropy. [INFERENCE]

## 4. Migration Without Citation Breakage

### 4.1 Documented patterns

Jira retains old issue keys as historical references after project-key changes and warns against reusing keys because doing so can redirect old references incorrectly. GitHub issue transfer preserves the old URL as a redirect, and repository rename also redirects issue URLs subject to documented exceptions. Git's hash transition design keeps a bidirectional object-name mapping while repositories move between hash formats. [VERIFIABLE][^1][^2][^3][^25]

These systems differ internally, but the externally visible rule is consistent: published identifiers become aliases that outlive the allocator that created them. None makes renumbering all old prose and commits a normal recovery action. [INFERENCE]

### 4.2 Proposed migration for this fleet

1. **Audit before cutover.** Build and validate a complete mapping from every existing `PREFIX-N` display ID to its canonical record/hash. If one old display ID already denotes two records, quarantine the ambiguity for an explicit human resolution; do not silently pick one. [HEURISTIC]
2. **Freeze the old allocator.** Set a format epoch and reject old-client attempts to mint new sequential display IDs after the cutover. Readers remain backward compatible. [INFERENCE]
3. **Enroll writers.** Give each machine a fleet-unique, immutable actor token and initialize a durable node-global counter. A token is tombstoned permanently when a machine is retired; it is never reassigned. [INFERENCE]
4. **Mint only the new grammar.** New records use `PREFIX-actor-sequence-check`, while the identity-bearing `(actor, sequence)` is unique across all projects because the sequence is node-global, not reset per repository. [INFERENCE]
5. **Retain aliases indefinitely.** The committed canonical dataset carries the old-display-ID-to-record mapping, and resolvers accept both grammars. Existing documents, filenames, branch names, and commits are not rewritten. [INFERENCE]
6. **Detect invariant failure.** On merge/import, if the same new ID maps to different canonical records, quarantine both and stop publication. Never auto-renumber an already emitted ID. [HEURISTIC]

The flag day applies only to **writers**. It does not require all citations or readers to change simultaneously. Mixed-version writers are the principal migration hazard; a stale binary that can still issue `max+1` IDs recreates the defect even after the new allocator ships. [INFERENCE]

## 5. Length Versus Human Usability

### 5.1 What can be measured

The security literature offers adjacent, not directly equivalent, evidence. In a 1,476-participant study, assigned three- and four-word passphrases took longer to enter than five- and six-character alternatives, though their security properties differed. A 12-week workplace field study found word-based passphrases produced more typographical errors but broadly comparable recall. [VERIFIABLE][^4][^5]

A 62-participant study of word-based key fingerprints found visual comparison more effective for non-security-critical errors, while verbal comparison was easier and imposed lower mental workload. [VERIFIABLE][^6] That supports evaluating words for speech but does not establish the best canonical issue-ID length. [INFERENCE]

No direct empirical study located in this run measures how reliably developers can type, speak, remember, and distinguish issue IDs composed of random words versus Crockford Base32 at realistic tracker lengths. No significant post-2024 development was found for this specific HCI question after searches of security-usability venues and identifier literature. [NO SOURCE]

### 5.2 Design implications

Entropy-per-character favors alphanumerics: Crockford Base32 carries five bits per symbol, whereas a typical 2,048-word token carries 11 bits but averages several letters plus a separator. Words favor chunking and speech; checksums favor immediate typo rejection. Human usability therefore has multiple dimensions, and “memorable” should not be reduced to the number of visible tokens. [INFERENCE]

For this use case, people usually copy, complete, search, or say an ID; they rarely memorize it as a secret. A compact structured ID with a check word and good resolver UX is likely better than paying the length of six random words for entropy. This is a design inference, not a measured issue-tracker result. [INFERENCE]

The 2026 Internet-Draft for Base32 for Humans codifies Crockford's ambiguity handling—accepting `I`/`L` as `1`, `O` as `0`, optional hyphens, and an optional modulo-37 checksum—but it is explicitly a work in progress with no standards status. Bech32 offers a stronger six-character checksum with documented substitution/error-detection properties, at the cost of too much overhead for a short prose ID. [VERIFIABLE][^26][^27]

## 6. Content- and Title-Derived Slugs

### 6.1 How name-based systems actually resolve collisions

WordPress's `wp_unique_post_slug` queries the current namespace and appends numeric suffixes when a slug conflicts. Kubernetes lets clients supply a name or ask the API server to append a generated suffix; `generateName` can still return a conflict after bounded server-side retries. PyPI normalizes package names by lowercasing and collapsing runs of punctuation, while npm organization scopes create centrally registered namespaces. [VERIFIABLE][^28][^29][^30][^31]

All four depend on a coordination point for canonical-name allocation. They show the common mitigation—human stem plus suffix or scope—but do not satisfy partitioned offline minting. [INFERENCE]

### 6.2 Stability under rename

An ID continuously derived from an issue title changes when the title changes, violating stability. Freezing the initial title slug avoids that but creates a permanent label that may become misleading, leak sensitive topic words, and still requires collision entropy. Appending a strong random or actor-sequence suffix makes the title decorative rather than identity-bearing. [INFERENCE]

Accordingly, title words belong in search aliases and UI, not in the canonical identifier. If a human stem is retained, parsing and equality should ignore it and bind identity to the suffix; otherwise later title edits create either renames or semantic drift. [INFERENCE]

## 7. Issue-Tracker and DVCS-Adjacent Prior Art

### 7.1 git-bug

git-bug describes itself as a distributed, offline-first bug tracker embedded in Git. Its entity ID implementation hashes serialized data with SHA-256 and defines a seven-character “human ID” prefix. [VERIFIABLE][^32][^33] This is direct precedent for candidate (e)'s architecture: canonical content-derived identity plus ergonomic abbreviation. [INFERENCE]

It does not make a fixed seven-hex-character citation globally collision-proof. A 28-bit prefix has an approximate 17.0% chance of at least one collision by 10,000 independent objects and effectively certain collision by 100,000; any safe abbreviation must lengthen or fail closed when ambiguous. [INFERENCE]

### 7.2 Radicle, Fossil, and Bugs Everywhere

Radicle's local-first collaboration protocol stores issues as signed Git-backed collaborative objects and roots repository identity in cryptographic keys; Fossil ticket timelines expose ticket UUIDs and change hashes. [VERIFIABLE][^34][^35] Both are precedent for durable cryptographic identity rather than short random words. [INFERENCE]

Bugs Everywhere is documented as a distributed bug tracker intended to work with distributed revision control. [VERIFIABLE][^36] A current primary source establishing the exact uniqueness algorithm of its issue identifiers was not located despite searches of its surviving documentation and repositories. [NO SOURCE]

### 7.3 Automerge and MongoDB as adjacent exact-pattern evidence

Automerge operation IDs combine an actor ID with a per-actor counter, and its documentation warns that concurrently using the same actor ID is an error. [VERIFIABLE][^37][^38] This is almost exactly the proposed identity tuple and makes the true invariant visible: actor IDs must be unique and allocator state must not be cloned. [INFERENCE]

MongoDB ObjectId is a 12-byte value containing a seconds timestamp, a five-byte process-random value, and a three-byte incrementing counter; its order is only approximate. [VERIFIABLE][^15] Because the process component is random, it remains in the negligible-risk rather than strict-partition family. [INFERENCE]

### 7.4 Landscape conclusion

The systems closest to the requested operating model choose one of two approaches: cryptographic canonical IDs with abbreviations/search, or actor/worker namespace plus local sequence. No located system obtains short, semantic, exact, offline uniqueness merely by choosing a clever random wordlist. [INFERENCE]

No located library can be adopted unchanged while meeting all five constraints: ULID/UUID/KSUID/NanoID remain probabilistic, Snowflake-family libraries add clock and worker assumptions, and git-bug/Radicle/Fossil retain cryptographic display identities. The reusable result is the actor-plus-counter architecture and standard human-oriented encoding/checksum practice, not a drop-in issue-ID package. [INFERENCE]

## 8. Gaps, Blindspots & Emergent Findings

This section deliberately follows findings outside the named candidate list.

### 8.1 Blindspot: “never” is conditional on machine identity and state

If two isolated generators can begin with identical complete state and receive identical inputs, no deterministic algorithm can prevent them from emitting the same ID. Randomness only changes certainty into probability. Therefore exact offline uniqueness requires an external invariant established before minting: disjoint actor namespaces, non-cloned counters, or equivalent hardware identity. [INFERENCE]

Backup restore, VM imaging, home-directory copying, and worktree cloning can duplicate both the actor token and sequence. The design must place allocator state outside ordinary repository copies, advance it transactionally, and define restore/re-enrollment behavior. If arbitrary rollback must also be tolerated without coordination or trusted non-rollback storage, constraint 1 is unsatisfiable as written. [INFERENCE]

### 8.2 Blindspot: machine onboarding is a coordination event

The recommended scheme removes coordination from the hot path, not from fleet administration. Actor tokens must be assigned from a registry, or derived from enough random entropy to reintroduce a nonzero collision probability. Retired tokens must be tombstoned forever. [INFERENCE]

This is a favorable trade if machines are enrolled rarely and issues are minted often, but the design decision should state it explicitly rather than calling the system coordination-free. [INFERENCE]

### 8.3 Blindspot: global chronology is a separate problem

No wall-clock identifier can promise a globally correct total creation order during partitions and clock skew. UUIDv7, ULID, KSUID, and Snowflake provide useful index locality or approximate order, not consensus chronology. If deterministic display order is needed, store `created_at` separately and tie-break by canonical ID; do not overload identity with causal semantics. [INFERENCE]

### 8.4 Blindspot: project prefixes and node counters

Fleet-wide uniqueness fails if an actor resets its counter per project while the same actor token appears under prefixes that can be renamed or collide. Make the counter node-global and the actor token fleet-global; treat `AIH` as routing/display metadata, not a uniqueness boundary. [INFERENCE]

### 8.5 Failure handling must preserve citations

Even a proof-oriented allocator needs duplicate detection at import and merge boundaries because implementations, storage, and deployment procedures can violate assumptions. A duplicate mapping must stop or quarantine ingestion and surface both records. “Pick a winner and renumber the loser” is forbidden after publication. [HEURISTIC]

### 8.6 Privacy and adversarial behavior

Timestamp IDs leak creation time; stable actor tokens reveal that two issues originated on the same machine; title slugs may reveal sensitive topics in logs and branch names. These may be acceptable for an internal tracker, but they should be consciously accepted. [INFERENCE]

The birthday table also assumes honest independent randomness. A malicious or broken client can deliberately reuse a random ID or actor/counter pair. Authentication and signed provenance can identify the violating writer, but no identifier grammar alone prevents Byzantine duplication. [INFERENCE]

### 8.7 Emerging practice and evidence gaps

UUID's latest standard is RFC 9562 from 2024, and Base32 for Humans has a 2026 Internet-Draft. [VERIFIABLE][^7][^26] No significant other post-2024 developments were found for Snowflake-family allocation, classic word-fingerprint algorithms, or issue-ID-specific HCI. [NO SOURCE]

This research countered anchor bias by adding Automerge actor/counter IDs, MongoDB ObjectIds, checksum encodings, allocator rollback/cloning, fleet enrollment, stale-writer cutover, privacy leakage, and Byzantine clients. The unresolved empirical blindspot is developer performance with candidate IDs in actual issue-tracker tasks; the available studies test passwords or fingerprints instead. [INFERENCE]

## 9. Comparison

Legend: **✓** satisfies; **△** conditionally or partially satisfies; **✗** fails. Scores are design judgments against ISSUE-REF's five constraints, not claims made by the cited systems. [INFERENCE]

| # | Candidate | 1. Offline fleet uniqueness | 2. Short / speakable / orderable | 3. Stable | 4. Human-distinguishable | 5. Citation-preserving migration | Overall |
|---:|---|---|---|---|---|---|---|
| 1 | (a) Random word slug, e.g. `ISSUE-REF` | ✗ Two words collide; any finite random form is probabilistic | △ Six–seven words are needed for a conservative bound | ✓ if frozen | ✓ words help speech, but similar words remain | ✓ aliases allow cutover | Reject as canonical |
| 2 | (b) Machine namespace + sequence, e.g. `ISSUE-REF` | △ Exact only with unique, never-reused machine token and non-rollback counter | ✓ compact and locally ordered | ✓ | △ adjacent numbers are easy to transpose | ✓ | Viable after hardening |
| 3 | (c) UUIDv7/ULID-style time + randomness | ✗ negligible, not zero | △ sortable but 26–36 characters and not memorable | ✓ | △ ambiguity-resistant alphabets help; tokens remain opaque | ✓ | Good probabilistic fallback |
| 4 | (d) Live title/content slug | ✗ titles collide | ✓ semantic and speakable | ✗ rename changes derivation | △ memorable but similar titles confuse | ✓ | Reject as identity; retain for search |
| 5 | (e) Content hash as only canonical ID + resolver UX | ✗ cryptographically negligible, not zero | ✗ raw hash fails the stated ergonomic constraint; abbreviation is variable | ✓ for immutable creation object | △ completion/search helps, spoken use remains poor | ✓ | Sound internal identity, poor sole display ID |
| 6 | **Hardened actor + node-global counter + check word**, e.g. `ISSUE-REF` | **✓ conditional on enrolled actor uniqueness and durable counter** | **✓ compact, speakable, locally ordered** | **✓** | **✓ actor/check words separate nearby sequences and reject many typos** | **✓ permanent aliases** | **Recommended** |
| 7 | 80-bit random Crockford Base32 + checksum | ✗ \(4.14\times10^{-15}\) at 100,000 draws, not zero | △ about 16 data symbols plus check symbol | ✓ | ✓ tolerant alphabet and checksum | ✓ | Best no-provisioning fallback |

Candidate 6 is candidate (b) with the assumptions made explicit and its human factors improved. The check word is not part of the uniqueness proof. If exact actor enrollment and durable counters cannot be guaranteed, candidate 7 is more honest than pretending a two-word slug is safe. [INFERENCE]

## 10. Recommendation

### 10.1 Pick

Adopt a **fleet-enrolled actor-token + durable node-global sequence + deterministic check-word** display ID. A concrete grammar is:

```text
<project>-<actor>-<sequence>-<check>
ISSUE-REF
```

The canonical uniqueness key is `(actor_id, sequence_uint64)`. The project prefix and rendered actor word support routing and speech; the check word is derived from a versioned, non-secret checksum of the complete canonical tuple and detects transcription mistakes. The existing content hash remains the canonical record-integrity/deduplication key and an alternate resolver key, but there is exactly one immutable display-ID mapping per record. [INFERENCE]

This is the only evaluated direction that can satisfy the literal requirement without a mint-time round trip, and it does so only under explicit operational invariants. It is shorter and more speakable than UUID/ULID, more collision-resistant than random word slugs in the strict sense, and more stable than title derivation. [INFERENCE]

### 10.2 Required invariants

The design is valid only if all of the following become testable acceptance criteria:

1. **Actor uniqueness:** enrollment assigns one immutable fleet-wide actor ID; clones must re-enroll; retired IDs are permanently tombstoned. [INFERENCE]
2. **Node-global monotonicity:** one actor counter spans every project and repository and never resets. Reserve ranges transactionally if needed for crash safety; gaps are valid. [INFERENCE]
3. **Rollback defense:** restoring repository data cannot restore allocator state. A restored machine either proves a high-water mark or re-enrolls under a new actor ID. [INFERENCE]
4. **Stable rendering:** actor and check dictionaries are versioned protocol data; canonical lowercase ASCII rendering never changes. [INFERENCE]
5. **Fail-closed collision handling:** importing two records with one display ID quarantines the conflict and never auto-renumbers either citation. [HEURISTIC]
6. **Writer epoch:** post-cutover clients refuse to mint the legacy numeric grammar, and stale writers are detected before their output can merge. [INFERENCE]
7. **Permanent aliases:** every pre-cutover display ID resolves indefinitely to the same canonical record. [INFERENCE]

### 10.3 Honest tradeoffs

The actor token exposes origin affinity, the sequence exposes per-actor issue volume, and IDs are only locally—not globally—ordered. Enrollment is a coordination point, and disaster recovery is harder than with pure random IDs. The scheme is also custom protocol work, so conformance vectors, parser fuzzing, and cross-platform filename tests are required. [INFERENCE]

If those tradeoffs are unacceptable, explicitly relax constraint 1 from “never” to a quantitative service objective and use at least 80 random bits encoded in Crockford Base32 plus a checksum. Do not substitute two or three random words: at 100,000 records their collision risk is demonstrably too high. [INFERENCE]

## 11. Open Questions

1. Is machine enrollment already backed by a fleet inventory with immutable IDs, and can it tombstone IDs permanently?
2. Which storage location can maintain a node-global counter across repository deletion, backup restore, OS reinstall, and worktree cloning?
3. Must a reimaged physical machine retain its actor token, or should every installation receive a new one?
4. Is origin-machine leakage acceptable in issue IDs, or should actor tokens be randomly assigned opaque words with the registry retaining the mapping?
5. What maximum actor-token, sequence, and check-word lengths fit existing branch, filename, parser, and UI constraints?
6. Which transcription errors must the checksum detect, and should the display use a word check symbol, Crockford mod-37, or a stronger code?
7. How will mixed-version writers be blocked at cutover when offline machines reconnect months later?
8. Do any of the approximately 900 historical numeric IDs already map to multiple hashes, and what is the explicit adjudication process if so?
9. Should the old numeric ID remain the preferred display for grandfathered records, or should UI show both old alias and new-style canonical ID?
10. What measured developer-task study—copy, speech, delayed recognition, branch naming, and typo recovery—will validate the final grammar before fleet rollout?

## Sources

[^1]: Atlassian. (2025). [Reset the project counter and reuse Jira issue keys after moving issues](https://support.atlassian.com/jira/kb/reset-the-project-counter-and-reuse-jira-issue-keys-after-moving-issues/). Jira Knowledge Base. Verified accessible (HTTP 200) 2026-08-09. (Documents historical keys and the danger of key reuse.)
[^2]: GitHub. (2026). [Transferring an issue to another repository](https://docs.github.com/en/enterprise-cloud@latest/issues/tracking-your-work-with-issues/administering-issues/transferring-an-issue-to-another-repository). GitHub Docs. Verified accessible (HTTP 200) 2026-08-09. (Documents redirects from old issue URLs.)
[^3]: Git Project. (2026). [Hash function transition](https://git-scm.com/docs/hash-function-transition.html). Git Documentation. Verified accessible (HTTP 200) 2026-08-09. (Defines SHA-1/SHA-256 translation and transition modes.)
[^4]: Shay, R., Kelley, P. G., Komanduri, S., Mazurek, M. L., Ur, B., Vidas, T., Bauer, L., Christin, N., & Cranor, L. F. (2012). [Correct horse battery staple: Exploring the usability of system-assigned passphrases](https://www.pdl.cmu.edu/PDL-FTP/associated/soups2012-passphrases_abs.shtml). SOUPS 2012. Verified accessible (HTTP 200) 2026-08-09. (Adjacent evidence on entry time and errors.)
[^5]: Keith, M., Shao, B., & Steinbart, P. J. (2007). [The usability of passphrases for authentication: An empirical field study](https://www.sciencedirect.com/science/article/abs/pii/S1071581906001236). International Journal of Human-Computer Studies, 65(1), 17–28. Verified accessible (HTTP 200) 2026-08-09. (Twelve-week field study.)
[^6]: Livsey, L., Petrie, H., Shahandashti, S. F., & Fray, A. (2021). [Performance and usability of visual and verbal verification of word-based key fingerprints](https://arxiv.org/abs/2106.01131). HAISA 2021 accepted manuscript. Verified accessible (HTTP 200) 2026-08-09. (62-participant fingerprint-comparison study.)
[^7]: Davis, K., Peabody, B., & Leach, P. (2024). [RFC 9562: Universally Unique IDentifiers (UUIDs)](https://www.rfc-editor.org/rfc/rfc9562.html). RFC Editor. Verified accessible (HTTP 200) 2026-08-09. (UUID layouts, collision considerations, and monotonicity.)
[^8]: ULID contributors. (2026). [Universally Unique Lexicographically Sortable Identifier specification](https://github.com/ulid/spec). GitHub. Verified accessible (HTTP 200) 2026-08-09. (48-bit timestamp, 80-bit randomness, and monotonic factory.)
[^9]: Segment. (2026). [KSUID](https://github.com/segmentio/ksuid). GitHub. Verified accessible (HTTP 200) 2026-08-09. (20-byte layout, Base62 encoding, and time sorting.)
[^10]: Twitter. (2010). [Snowflake IdWorker.scala](https://raw.githubusercontent.com/twitter-archive/snowflake/snowflake-2010/src/main/scala/com/twitter/service/snowflake/IdWorker.scala). GitHub archive. Verified accessible (HTTP 200) 2026-08-09. (Original worker/datacenter/sequence allocation and clock checks.)
[^11]: Sony. (2026). [Sonyflake](https://github.com/sony/sonyflake). GitHub. Verified accessible (HTTP 200) 2026-08-09. (Bit allocation and machine-ID assumptions.)
[^12]: Instagram Engineering. (2011). [Sharding & IDs at Instagram](https://instagram-engineering.tumblr.com/post/10853187575/sharding-ids-at-instagram). Instagram Engineering. Verified accessible (HTTP 200) 2026-08-09. (Time, logical-shard, and sequence design.)
[^13]: Nano ID contributors. (2026). [Nano ID](https://github.com/ai/nanoid). GitHub. Verified accessible (HTTP 200) 2026-08-09. (Default alphabet, length, and random generation.)
[^14]: Parallel Drive. (2026). [Cuid2](https://github.com/paralleldrive/cuid2). GitHub. Verified accessible (HTTP 200) 2026-08-09. (Default length and mixed-input hash construction.)
[^15]: MongoDB. (2026). [BSON types: ObjectId](https://www.mongodb.com/docs/manual/reference/bson-types/#objectid). MongoDB Manual. Verified accessible (HTTP 200) 2026-08-09. (ObjectId fields and approximate ordering.)
[^16]: Chacon, S., & Straub, B. (2026). [Git internals: Git objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects). Pro Git. Verified accessible (HTTP 200) 2026-08-09. (Content-addressed object identity and object format.)
[^17]: Docker. (2026). [Running containers](https://docs.docker.com/engine/containers/run/). Docker Documentation. Verified accessible (HTTP 200) 2026-08-09. (Random container UUIDs and generated friendly names.)
[^18]: Salesforce. (2026). [Heroku app names and subdomains](https://devcenter.heroku.com/articles/app-names-and-subdomains). Heroku Dev Center. Verified accessible (HTTP 200) 2026-08-09. (Generated names, uniqueness, random hostname suffix, and rename behavior.)
[^19]: what3words. (2026). [How does what3words work?](https://support.what3words.com/en/articles/1520386). what3words Support. Verified accessible (HTTP 200) 2026-08-09. (Predetermined three-word mapping.)
[^20]: what3words. (2026). [Mobile Offline SDK](https://developer.what3words.com/enterprise-suite/mobile-offline-sdk). what3words Developer Portal. Verified accessible (HTTP 200) 2026-08-09. (Offline lookup and AutoSuggest support.)
[^21]: Eckersley, P. (2016). [EFF's new wordlists for random passphrases](https://www.eff.org/deeplinks/2016/07/new-wordlists-random-passphrases). Electronic Frontier Foundation. Verified accessible (HTTP 200) 2026-08-09. (List size, word length, curation, and usability limitations.)
[^22]: Haller, N., Metz, C., Nesser, P., & Straw, M. (1998). [RFC 2289: A one-time password system](https://www.rfc-editor.org/rfc/rfc2289.html). RFC Editor. Verified accessible (HTTP 200) 2026-08-09. (Six-word encoding of 64 bits plus checksum.)
[^23]: McDonald, D. L. (1994). [RFC 1751: A convention for human-readable 128-bit keys](https://www.rfc-editor.org/rfc/rfc1751.html). RFC Editor. Verified accessible (HTTP 200) 2026-08-09. (Twelve-word representation and language caveat.)
[^24]: Zimmermann, P. (1995). [PGP Word List](https://philzimmermann.com/docs/PGP_word_list.pdf). Verified accessible (HTTP 200 PDF) 2026-08-09. (Alternating phonetic wordlists and error-detection rationale.)
[^25]: GitHub. (2026). [Renaming a repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository). GitHub Docs. Verified accessible (HTTP 200) 2026-08-09. (Redirect behavior and limits.)
[^26]: Crockford, D., & Davis, K. R. (2026). [Base32 for Humans, Internet-Draft -01](https://datatracker.ietf.org/doc/draft-crockford-davis-base32-for-humans/). IETF Datatracker. Verified accessible (HTTP 200) 2026-08-09. (Ambiguity rules, optional checksum, and draft status.)
[^27]: Wuille, P., & Maxwell, G. (2017). [BIP 173: Base32 address format for native v0-16 witness outputs](https://bips.dev/173/). Bitcoin Improvement Proposals. Verified accessible (HTTP 200) 2026-08-09. (Bech32 alphabet and checksum error properties.)
[^28]: WordPress.org. (2026). [wp_unique_post_slug()](https://developer.wordpress.org/reference/functions/wp_unique_post_slug/). WordPress Developer Resources. Verified accessible (HTTP 200) 2026-08-09. (Database-checked numeric slug suffixes.)
[^29]: Kubernetes Authors. (2026). [Object names and IDs](https://kubernetes.io/docs/concepts/overview/working-with-objects/names/). Kubernetes Documentation. Verified accessible (HTTP 200) 2026-08-09. (Names, UIDs, `generateName`, and conflict retries.)
[^30]: Python Packaging Authority. (2026). [Names and normalization](https://packaging.python.org/en/latest/specifications/name-normalization/). Python Packaging User Guide. Verified accessible (HTTP 200) 2026-08-09. (Central package-name normalization rules.)
[^31]: npm, Inc. (2026). [About organization scopes and packages](https://docs.npmjs.com/about-organization-scopes-and-packages/). npm Docs. Verified accessible (HTTP 200) 2026-08-09. (Centrally registered scoped names.)
[^32]: git-bug contributors. (2026). [git-bug](https://github.com/git-bug/git-bug). GitHub. Verified accessible (HTTP 200) 2026-08-09. (Distributed offline-first issue-tracker architecture.)
[^33]: git-bug contributors. (2026). [entity/id.go](https://github.com/git-bug/git-bug/blob/trunk/entity/id.go). GitHub. Verified accessible (HTTP 200) 2026-08-09. (SHA-256 derivation and seven-character human prefix.)
[^34]: Radicle. (2026). [How Radicle works](https://radicle.xyz/guides/protocol/). Radicle Guides. Verified accessible (HTTP 200) 2026-08-09. (Local-first signed Git collaboration and collaborative objects.)
[^35]: Fossil SCM. (2026). [JSON API: Timeline](https://fossil-scm.org/home/doc/2a7b1de3/www/json-api/api-timeline.md). Fossil Documentation. Verified accessible (HTTP 200) 2026-08-09. (Ticket UUID and artifact hash fields.)
[^36]: Bugs Everywhere contributors. (2012). [Bugs Everywhere documentation](https://bugs-everywhere.readthedocs.io/). Read the Docs. Verified accessible (HTTP 200) 2026-08-09. (Distributed tracker purpose; exact current ID algorithm not established.)
[^37]: Automerge contributors. (2026). [Automerge binary format specification](https://automerge.org/automerge-binary-format-spec/). Automerge Documentation. Verified accessible (HTTP 200) 2026-08-09. (Operation IDs as actor ID plus counter.)
[^38]: Automerge contributors. (2026). [ActorId](https://automerge.org/automerge-swift/documentation/automerge/actorid/). Automerge Swift Documentation. Verified accessible (HTTP 200) 2026-08-09. (Warning against concurrent actor-ID reuse.)

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

**Registry ID:** `ISSUE-REF` (wrapper-supplied research target; no separate research-registry ID provided)

**Model:** GPT-5 Codex (flagship, high effort)

**Date:** 2026-08-09

```text
RESEARCH TASK: Collision-resistant, human-readable issue-ID schemes for a distributed multi-machine work-tracking system.

CONTEXT (measured 2026-08-09 17:30 PT). This feeds a Beads (bd) issue-tracker design decision
tracked as ai-harness issue ISSUE-REF (hash ISSUE-REF). Verbatim problem statement from that issue,
already fully diagnosed — do not re-derive it, treat it as ground truth to design against:

"The sequential numeric display id (ISSUE-REF, ISSUE-REF) is the root cause of a recurring,
expensive class of fleet failures... A display id must be unique fleet-wide, but it is minted as
max(N)+1 from whatever the local Dolt store happens to hold, which never reads the committed
.beads/issues.jsonl. So the allocator's INPUT is a per-machine mutable cache while its OUTPUT is a
fleet-wide unique name. The flock around it is correct and irrelevant: it serializes writers
within one store; the collision is across stores. Any repair is valid only until the next bd-new
on a trailing store. This is not a bug to be patched — max+1 over a partial view is unfixable
without a coordination point the fleet does not have."

WHAT A REPLACEMENT MUST SATISFY (from ISSUE-REF, the constraints that killed the current design —
a proposal missing any one of these is not an improvement):

1. Mintable OFFLINE with no shared lock, no coordination round-trip, and no read of any remote —
   N machines minting concurrently while network-partitioned must never produce the same id.
2. Short enough to live in prose, commit subjects, branch names and doc filenames. A raw content
   hash (e.g. a git-style SHA prefix) already satisfies uniqueness and was rejected on exactly
   this axis: not speakable, not memorable, not orderable.
3. Stable once cited. Renumbering must be a never, not a recovery step.
4. Human-distinguishable — two ids seen a week apart should be hard to confuse, which pure random
   hex is bad at.
5. No fleet-wide renumber required to adopt it, and a migration that preserves every existing
   citation (~900 existing records, cited in commit messages, doc filenames, and prose).

CANDIDATE DIRECTIONS ALREADY ON THE TABLE (evaluate these AND actively search for better ones —
this list is a floor per the independent-exploration instructions below, not the answer):

(a) Short word-slug from a curated wordlist, minted from local entropy (e.g. "ISSUE-REF").
(b) Machine-namespaced sequence (e.g. "ISSUE-REF" where s7 is a per-machine token).
(c) Timestamp-encoded base32/base62, monotonic and sortable (ULID-family style).
(d) Content-derived slug from the issue title (memorable but unstable under retitling, can
    collide).
(e) Keep a content hash as the ONLY id and fix ergonomics instead (better resolver support, shell
    completion, title-search-first workflow) — removing the second, colliding namespace entirely
    rather than replacing it.

RESEARCH QUESTIONS (a floor, not a checklist — see independent-exploration instructions below):

1. How do widely-used distributed or offline-first systems mint globally-unique identifiers
   without a coordination point? Survey UUID (v4 and the newer v7/v6 time-ordered variants),
   ULID, KSUID, Twitter Snowflake and its descendants (Sonyflake, Instagram's variant), NanoID,
   cuid2, and Git's own content-addressed SHA. For each: collision probability at realistic
   volumes, whether it requires any coordination/clock-sync assumption, and monotonic-sortability
   properties.
2. How do systems that prioritize HUMAN memorability solve unique naming without central
   coordination? Survey Docker's random container-name generator (adjective+surname), Heroku's
   app-name generator, what3words-style geocoding, Diceware/EFF wordlists (used for
   passphrases — same "encode entropy as words" problem), PGP/SSH key fingerprint word-encoding
   schemes (e.g. the classic S/KEY and PGP word list algorithms), and any prior art specifically
   for bug/issue-tracker ids that are both memorable and collision-resistant.
3. For a word-slug approach specifically: what collision math applies (treat as a birthday-problem
   calculation), what wordlist size and word-count is needed to keep collision probability
   negligible at a stated realistic volume (on the order of 1,000-100,000 issues minted from
   dozens of concurrent uncoordinated machines over a period of years), and what practical
   wordlist curation pitfalls exist (homophones, profanity, length variance, non-English-speaker
   ergonomics, case-sensitivity issues in filenames/URLs).
4. How do systems handle MIGRATION from a centralized/sequential id scheme to a decentralized one
   without invalidating already-published citations? Survey any documented real-world precedent
   (e.g. a project that moved between issue trackers or changed its numbering scheme) for the
   pattern used: grandfathering old ids indefinitely, a translation/alias table, a one-time
   flag-day cutover, or a hybrid where new and old coexist.
5. What is the actual, measured tradeoff between id LENGTH and both collision-resistance and
   human usability? Is there empirical or HCI research on how many random word-tokens or
   alphanumeric characters humans can reliably type, say aloud, and distinguish from a similar
   neighbor, relevant to picking a concrete scheme length?
6. Content-derived/title-slug schemes (candidate (d) above): survey how systems that DO use a
   content-derived slug (e.g. URL slugs in blogging platforms, npm/PyPI package names, Kubernetes
   resource names) handle the instability-under-rename and collision problem in practice — is
   there a standard mitigation (append a short random suffix, hash-of-title fallback on
   collision) worth evaluating as a hybrid with (a)?
7. Are there existing open-source or commercial issue-tracker / distributed-VCS-adjacent tools
   that have ALREADY solved this exact problem (globally-unique, human-readable, offline-mintable
   issue ids across many uncoordinated clients) that this project could learn from or even adopt
   a library/algorithm from directly, rather than designing one from scratch?

## Scope note — questions, examples, and named references are a starting point, not a checklist
The questions, topics, and named examples below are illustrative anchors and a FLOOR for this
research — not an exhaustive list to answer only or evaluate only. Reason independently: survey the
landscape broadly, follow the evidence where it leads, expand scope where warranted, and surface
relevant work, factors, and failure modes not named here. Actively resist answering only the listed
questions or evaluating only the named approaches — an output that merely fills in the listed items
has NOT met the research goal.

## Independent exploration (gaps, blindspots, emergent threads) — required
Treat the question list as a FLOOR, not a ceiling. As you research, actively surface what this
framing may be missing and pursue each promising thread to a logical conclusion:
- Adjacent or upstream factors the questions don't capture.
- Contrarian / disconfirming evidence — report it even when it challenges the premise.
- Emerging 2025-2026 practices, tools, or research not anchored by the named examples.
- Known failure modes and second-order effects.
Whenever a load-bearing thread surfaces mid-research, follow it to its conclusion and report it in a
dedicated "Gaps, blindspots & emergent findings" subsection. Explicitly NAME any blindspot you
suspect but cannot resolve (and why) rather than omitting it. Anchor bias — over-fitting to the
listed questions and example approaches — is a known failure mode; counter it deliberately and say
where you did.

<grounding_instructions>
You are a principal distributed-systems engineer who has designed identifier schemes for
multi-region, offline-first, and eventually-consistent systems in production (the kind of person
who has personally debugged a collision incident from an under-specified ID scheme, and has
strong evidence-backed opinions about tradeoffs between coordination cost, collision probability,
and human usability). When you cannot find a source, you say so explicitly rather than guessing.

Temporal scope: this is a mix of evergreen distributed-systems theory (UUID/ULID/Snowflake-family
schemes, birthday-problem collision math) and current tooling/practice. Weight practical tooling
and library choices by recency — 2026 (primary) -> 2025 -> 2024 — but do not discount
foundational algorithmic papers or long-standing prior art (e.g. Twitter's original Snowflake
writeup, PGP word lists) merely for being older; cite them with their original date and treat them
as background/foundational rather than time-boxing them out. If post-2024 literature is genuinely
sparse for a subtopic, state "[subtopic]: no significant post-2024 developments found" rather than
backfilling with older sources presented as if current. Backfilling is a failure mode, not a hedge.

Before generating your final output, execute a Chain-of-Verification (CoVe) to ensure factual
fidelity over compliance.

Inside your thought process:
1. Isolate the core facts required.
2. Draft a tentative response.
3. Hostile Cross-Examination: flag any claim where you are citing a source because the prompt
   implied you should, rather than because you verified it.
4. Strip away any claim that cannot be empirically verified.

When generating your final output, classify every major claim. Write your rationale before
appending the tag -- writing the tag first causes post-hoc rationalization. Rationale -> evidence
check -> tag.

- [VERIFIABLE]: backed by documentation, peer-reviewed research, or official tech blogs
  (2024-2026, or foundational/original-source material regardless of date). Carry an inline
  footnote ref to the source: [VERIFIABLE][^N].
- [HEURISTIC]: widely accepted best practice without a specific citation.
- [INFERENCE]: a logical conclusion drawn from context. Provide your reasoning in-text. Do not
  fabricate a source. Tier tag only -- NO footnote ref.
- [NO SOURCE]: explicitly state when you cannot find verifiable data. Tier tag only -- NO
  footnote ref.

Citation format (mandatory for every externally-sourced claim):
- Inline: append the GFM footnote ref directly after the tier tag -- [VERIFIABLE][^N]. A claim
  citing multiple sources carries ascending separate refs -- [VERIFIABLE][^3][^7] (never grouped
  [^3,^7]; never out of order).
- Footnote definitions live once, under ## Sources, in APA form with a clickable URL or DOI and an
  access-verification stamp. Worked example:
    [^1]: LangChain. (2026). [Threads](https://docs.langchain.com/langsmith/threads). LangSmith
    Documentation. Verified accessible (HTTP 200) 2026-06-03. (Scope note.)
- URL-or-DOI ALWAYS: every source entry carries a clickable URL or a doi.org link -- paywalled/
  gated is fine (link it anyway; stamp the access status). Only the truly-irreducible case (no
  online catalog presence anywhere) gets an explicit [no online source located] marker with a
  one-line justification.
- Integrity: footnote refs are contiguous from [^1], every [^N] ref has a matching definition, and
  every definition is referenced -- no gaps, no orphans.
- [INFERENCE] / [NO SOURCE] claims carry the tier tag with NO footnote ref.

Hard constraint (overrides all formatting preferences): never invent a citation to satisfy a
formatting instruction. Accuracy > completeness.

For each load-bearing claim, require at least one web_search or web_fetch call; you must not
return [VERIFIABLE] without a URL you actually fetched in this session. Do not stop after one
round. If no source is found for a sub-question, run additional searches across alternate framings
before tagging it [NO SOURCE] -- that tag is reserved for genuine evidence absence, not retrieval
avoidance.

Format diagrams using Mermaid.js or ASCII. Format math using LaTeX. NEVER generate binary images.
</grounding_instructions>

## Required deliverable shape

Write the finished research document directly into the file already scaffolded at
docs/research/collision-resistant-human-readable-issue-ids.md (already conforms to the canonical
research-1.2.0 template's frontmatter and region markers -- fill the "body" region and the two
appendix regions, do not alter the region marker comments or the frontmatter template_version).
Follow the canonical structure: Table of Contents, Context (already filled, immutable, do not
edit), Executive Summary (3-5 numbered findings), numbered topic sections per the research
questions above (plus any independent-exploration findings as their own section), a Comparison
section scoring the candidate schemes above (plus any new ones you find) against the five
constraints from ISSUE-REF with a numbered "#" column, a Recommendation section (a clear pick with
honest tradeoffs -- this recommendation is an INPUT to the design doc, not a final decision),
an Open Questions section, a Sources section (GFM footnote definitions in APA form), an
"Appendix: Research Prompt" section containing this exact prompt verbatim in a fenced text block
with Registry ID, Model, and Date fields filled, and a Run History entry.

End your final chat message (not the file) with: the recommended candidate direction in one
sentence, confidence level, and the file path you wrote to. Under 150 words.
```

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

| Load-bearing claim | Primary source | Verbatim anchor (≤10 words) | Verification verdict |
|---|---|---|---|
| UUIDv4 has 122 random bits | [RFC 9562](https://www.rfc-editor.org/rfc/rfc9562.html) | “122 bits of pseudorandom data” | Confirmed in fetched standard |
| ULID has 48 timestamp and 80 random bits | [ULID specification](https://github.com/ulid/spec) | “48 bit timestamp” / “80 bits of randomness” | Confirmed in fetched specification |
| Snowflake partitions by worker and sequence | [Original IdWorker](https://raw.githubusercontent.com/twitter-archive/snowflake/snowflake-2010/src/main/scala/com/twitter/service/snowflake/IdWorker.scala) | “worker id bits” / “sequence bits” | Confirmed in fetched original source |
| EFF long wordlist contains 7,776 entries | [EFF wordlist rationale](https://www.eff.org/deeplinks/2016/07/new-wordlists-random-passphrases) | “our new list has 7776 words” | Confirmed in fetched primary publication |
| Jira keeps historical keys and warns on reuse | [Atlassian knowledge base](https://support.atlassian.com/jira/kb/reset-the-project-counter-and-reuse-jira-issue-keys-after-moving-issues/) | “historic issue key will still point” | Confirmed in fetched vendor documentation |
| git-bug derives entity IDs with SHA-256 | [git-bug `entity/id.go`](https://github.com/git-bug/git-bug/blob/trunk/entity/id.go) | “sha256.Sum256” | Confirmed in fetched project source |
| Automerge operations use actor ID plus counter | [Automerge binary format](https://automerge.org/automerge-binary-format-spec/) | “actor ID and a counter” | Confirmed in fetched specification |
| MongoDB ObjectId has timestamp, process random, counter | [MongoDB BSON documentation](https://www.mongodb.com/docs/manual/reference/bson-types/#objectid) | “5-byte random value” / “3-byte incrementing counter” | Confirmed in fetched vendor documentation |

The collision probabilities were independently recomputed from the birthday formula shown in
Section 1.1. Recommendation and scoring claims are explicitly tagged `[INFERENCE]`; they are not
presented as conclusions of the cited systems.

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

### 2026-08-09 — Primary-source synthesis

- **Model:** GPT-5 Codex (flagship, high effort)
- **Mode:** Scoped-write research; only this designated artifact was modified.
- **Method:** Canonical research-1.2.0 grounding workflow; multi-round primary-source retrieval;
  independent birthday-bound calculations; hostile citation and assumption review.
- **Result:** Recommended a hardened actor-token plus durable node-global sequence and check word,
  with permanent legacy aliases and an explicit probabilistic fallback.

<!-- /doc:region name="run_history" -->
