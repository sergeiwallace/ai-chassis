---
title: "Web Research — Literature Discovery, Bot-Block Workarounds & Citation Validation"
category: procedures
tags: [web, fetch, bot-block, 403, cloudflare, citation, provenance, research, literature-search, openalex]
status: active
source: maintainer
---

# Web Research — Literature Discovery, Bot-Block Workarounds & Citation Validation

## Discovering the literature (search the academic indices FIRST, and don't satisfice)

For any citation-rigorous or research-heavy task, **search the peer-reviewed literature
through the scholarly indices before the open web.** General web search — including the
WebSearch tool and most LLM browse tools — ranks SEO-optimized pages (Wikipedia, news,
RFE/RL, think-tank pages, blogs), **not** journal articles or monographs. Specialist
journals (e.g., the *Journal of Slavic Military Studies*) and academic books barely surface
there but sit right in the indices below. Relying on general search produces a "good enough
to cite" source while the authoritative body of work stays invisible. For example, a
specialist literature on regional water governance can be missed by web search and found
instantly in OpenAlex.

**The indices (clean JSON, no WAF, the programmatic equivalent of a Google Scholar library):**

- **OpenAlex** — best for topic discovery. Returns title/authors/year/venue, a citation count
  (`cited_by_count`) to gauge authority, and a reconstructable abstract.
  - Topic by title, ranked by influence:
    `api.openalex.org/works?filter=title.search:<phrase>&sort=cited_by_count:desc&per_page=8`
  - Relevance ranking (match quality): `?search=<phrase>` with **no** sort.
  - **Gotcha:** do NOT combine a loose `?search=<words>` with `sort=cited_by_count:desc` — it
    ignores topical relevance and returns unrelated mega-cited blockbusters (Lancet, IPCC,
    etc.). Use `filter=title.search:` for tight topical hits, or relevance (no sort).
  - **Read the abstract before citing:** add `&select=title,publication_year,cited_by_count,authorships,abstract_inverted_index`,
    then invert the position map (`{word: [positions]}` → join words by position) to read what
    the paper actually claims.
- **Crossref** — `api.crossref.org/works?query.bibliographic=<title or topic>&rows=8` to locate
  a specific work or confirm exact title/author/journal/year/DOI.
- **Semantic Scholar** — `api.semanticscholar.org/graph/v1/paper/search?query=<topic>&fields=title,year,citationCount,abstract,authors`
  for the citation graph and influential-citation flags; good for snowballing from one key paper.

**Two non-negotiables:**

1. **Don't satisfice.** One defensible source is not the goal. Sweep the top results by
   citation count, scan the body of work, and snowball through references and citing papers.
   Stopping at the first neutral scholar is a recall failure.
2. **Don't bare-cite (read, then cite the claim).** Pull the abstract (or full text via the
   ladder below) and tie every citation to the **specific figure or claim it supports**. A name
   dropped without engagement is the "citation-as-credential" AI tell and weak provenance; if a
   source carries no usable claim, drop it rather than padding the list. Reading often *changes*
   the claim — e.g., a peer-reviewed journal version with more careful numbers than the working
   paper you started from.

Once you've found the work, use the ladder below to read it when the publisher bot-blocks you.

## Web Fetch — Bot-Block Workarounds & Citation Validation

When fetching a source for research, citation validation, or claim provenance, a
`403 Forbidden` or a giant (~835 KB) HTML body with no article text is **almost
never a dead link** — it's a WAF/bot-block (Cloudflare, Akamai, PerimeterX) on a
**live** page. Do not record such a URL as dead, and do not conclude a cited paper
was hallucinated, until you have walked the workaround ladder below.

This pairs with the citation-validation / claim-provenance practice (the aido
provenance model: every load-bearing claim → a **verbatim quote** from the fetched
source → a **verdict** of SUPPORTED / PLAUSIBLE / UNVERIFIABLE / CONTRADICTED, plus a
**live-URL check**). The point of the ladder is to reach real source text (or
authoritative metadata) so the verdict is grounded, not guessed.

## The workaround ladder (try in order)

### 1. Metadata APIs — no WAF, clean JSON (the gold standard against hallucination)

These confirm a work **exists** with its exact title, authors, journal, year, and
DOI even when the publisher page blocks every scraper. Use them first for anything
with a DOI or a known title — they settle "is this citation real?" definitively.

- **Crossref:** `https://api.crossref.org/works/{doi}` → `message.title`,
  `message.author[]`, `message.container-title`, `message.issued`. Send a UA with a
  mailto for the polite pool, e.g. `-H 'User-Agent: provenance-check/1.0 (mailto:you@example.com)'`.
- **OpenAlex:** `https://api.openalex.org/works?filter=title.search:<words>` →
  title/author/year, and `abstract_inverted_index` (reconstructable to the abstract
  text). Good when you have a title but no DOI.
- **NBER:** working-paper pages (`nber.org/papers/wXXXXX`) resolve by title via a
  plain GET; the `<title>` tag confirms the paper. NBER **Digest** pages
  (`nber.org/digest/...`) often summarize findings in fetchable prose.
- **DataCite** (`api.datacite.org`) for datasets/preprints with DOIs.

Example (confirm a batch of DOIs are real, not hallucinated):

```bash
curl -s "https://api.crossref.org/works/10.1257/aer.20181289" \
  -H 'User-Agent: check/1.0 (mailto:you@example.com)' \
  | python3 -c "import sys,json;m=json.load(sys.stdin)['message'];print(m['title'][0],'|',', '.join(a['family'] for a in m['author'][:3]),'|',m['container-title'][0],'|',m['issued']['date-parts'][0][0])"
```

### 2. Full-header GET with a real browser User-Agent

Many WAFs 403 a bare `curl` but pass a request that looks like Chrome. A `HEAD`
often 403s where a full `GET` 200s — always retry blocked HEADs with GET. Clears
JSTOR, Pew, NBER, most university and gov sites:

```bash
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
curl -sL --compressed --max-time 30 -A "$UA" \
  -H 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8' \
  -H 'Accept-Language: en-US,en;q=0.9' \
  -H 'Referer: https://www.google.com/' \
  -H 'sec-ch-ua: "Chromium";v="126", "Google Chrome";v="126"' \
  -H 'Upgrade-Insecure-Requests: 1' \
  "$URL"
```

Then grep the body for an expected term (title/author/figure) to confirm you got
real content, not a challenge page.

### 2.5. Wayback Machine raw-file fetch (`id_` suffix) — beats WAFs that block even full-header curl

The Internet Archive sits **outside the origin's WAF**, so if a page or PDF was ever
crawled, you can pull the archived copy even when the live origin 403s *every* request
— including the full-header GET in step 2. This is the highest-yield move for
**WAF-blocked PDFs** (gov/think-tank reports behind Cloudflare/Akamai), which often
defeat headers entirely.

The key is the `id_` (identity) suffix on the snapshot timestamp: it returns the
**original raw bytes** (the actual PDF/HTML) with no Internet Archive toolbar or link
rewriting injected — essential for binary files like PDFs.

```bash
ORIG="https://www.example.gov/report.pdf"   # the live URL that 403s
# 1) find the closest snapshot (this API is itself un-WAF'd, clean JSON):
curl -s "http://archive.org/wayback/available?url=$ORIG" \
  | python3 -c "import sys,json;d=json.load(sys.stdin)['archived_snapshots'];print(d.get('closest',{}).get('url','NONE'))"
# 2) fetch the RAW original by inserting 'id_' right after the timestamp:
#    .../web/<timestamp>id_/<original-url>
curl -sL "http://web.archive.org/web/20251014060916id_/$ORIG" -o out.pdf
file out.pdf && pdftotext out.pdf - | grep -i "<expected figure>"
```

Without `id_` (a normal `/web/<ts>/` URL) you get the IA-wrapped HTML viewer, which
corrupts a PDF download — always use `id_` for raw files. If the availability API
returns no snapshot, the page may still be archivable on demand, but move on to step 3.

### 3. CDP logged-in browser — for JS-rendered / hard-Cloudflare pages

Some sites (ScienceDirect, MIT Press Direct, heavy-Cloudflare) serve `curl` a
**contentless JS-challenge shell** (a large body with no article text) no matter the
headers — the content only materializes after JS runs. Use the real browser:

- `ai cdp start` (Chrome on `:9222`), then `agent-browser connect 9222` /
  `agent-browser open <url>` / `agent-browser eval '<js to read the DOM>'`.
- The session can be logged in (e.g. to a publisher via institutional access, or to
  X/Twitter), which also clears auth-gated pages.

### 4. Mirrors & secondaries when the publisher is unreadable

If the canonical page stays unreadable, confirm existence via API (step 1) and read
the **abstract/findings** from an accessible mirror:

- **RePEc / IDEAS** (`ideas.repec.org`) — abstracts for most econ journal articles.
- **SSRN / NBER / arXiv working-paper** versions of the same paper.
- **OpenAlex** `abstract_inverted_index`; **Semantic Scholar** (`api.semanticscholar.org`).
- For news/think-tank pages: the outlet's AMP version, the Wayback Machine
  (`web.archive.org` — see step 2.5 for the raw `id_` fetch), or a reputable
  syndication (e.g. an article reprinted on RealClearPolicy). Note the secondary
  in the provenance record.

## Recording the result (provenance verdicts)

- **LIVE** — fetched real content directly.
- **BLOCKED-BUT-LIVE** — page blocks scrapers but existence/metadata confirmed via
  API or browser. Treat as reachable, not dead.
- **DEAD** — only after the full ladder fails (API has no record AND no mirror AND
  the browser gets a true 404). A genuine 404 + no metadata record is the signal a
  citation may be hallucinated — flag it, fix or drop the claim.

For each load-bearing claim, capture the **verbatim quote** that supports it (from
the source or an authoritative mirror/abstract) and assign SUPPORTED / PLAUSIBLE /
UNVERIFIABLE / CONTRADICTED. Surface CONTRADICTED loudly; never upgrade an
unreachable source to a false SUPPORTED.

## Anti-patterns

- ❌ Recording a publisher `403` as a dead link. (It's a bot-block — climb the ladder.)
- ❌ Concluding a paper is hallucinated because the page won't load. (Check Crossref/OpenAlex first.)
- ❌ Marking a claim SUPPORTED when you only reached a JS-challenge shell. (No quote = not SUPPORTED.)
- ❌ Hammering an API without a UA/mailto. (Use the polite pool; it's faster and won't rate-limit.)
- ❌ Citing a generic page (e.g. a Wikipedia article) as the source for a specific
  figure it doesn't actually contain. (Point the citation at the page that has the number.)
