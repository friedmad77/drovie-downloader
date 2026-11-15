# Recorded responses

Real answers from the real services, recorded once so the suite never opens a
socket. That is the point: a test that talks to TMDb fails when TMDb is slow,
when the key is rotated, and on a laptop on a train — and then nobody runs it.

Recorded 23 Aug 2026 against the live services from this machine, with `curl`,
by the same requests the processor makes. Nothing was edited except where a
file is marked synthetic below. **No credential appears in any body** — both
travel as query parameters, and only the response bodies are stored.

| File | Request | Status |
| --- | --- | --- |
| `plex_sections.xml` | `GET {PLEX_URL}/library/sections` | 200 |
| `plex_sections_401.html` | the same with a bad token | 401 |
| `plex_refresh_404.html` | `POST /library/sections/9999/refresh` | 404 |
| `tmdb_movie_search.json` | `search/movie?query=Blade Runner 2049&year=2017` | 200 |
| `tmdb_tv_search.json` | `search/tv?query=Severance&first_air_date_year=2022` | 200 |
| `tmdb_movie_no_results.json` | `search/movie?query=zzzzqqqqxxxxnotathing` | 200 |
| `tmdb_invalid_key.json` | `search/movie` with a made-up key | 401 |
| `tmdb_movie_no_date.json` | **synthetic** — see below | 200 |

`tmdb_movie_no_date.json` is `tmdb_movie_search.json` with `release_date`
removed from the first result. TMDb does return results without one — an
unreleased film, mostly — but not reliably enough to record on demand, and the
case matters: bash's `cut -d- -f1` on an empty date yields an empty year, and
the film is then filed into a directory called `Title ()`.

Two shapes are worth noticing when reading these.

**Plex answers errors in HTML, not XML.** A 401 and a 404 are both a bare
`<html><head><title>…` page. `curl -s` exits 0 for both, which is why
`scan_plex_library` reports a scan it never triggered as successful.

**Plex emits one `<Directory>` per line.** `get_plex_library_id`'s
`grep`-and-`head -1` pipeline depends on that without saying so; fold the same
document onto one line and it returns two ids joined by a newline. See the
`putio.plex` module docstring.
