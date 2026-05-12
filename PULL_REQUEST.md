# Add memory allocator configuration to resolve RSS creep

Replaces the system glibc `malloc` (default Chainguard Wolfi allocator) with
a tuned multi-allocator stack targeting each component in the dashboard's
data pipeline: Polars → Pandas → PyArrow → `st.cache_data`.

---

## Problem

A Streamlit dashboard in Docker on Kubernetes exhibited a monotonically
growing RSS — memory never returned to baseline even after days of idle.
After 4 usage cycles the container RSS drifted +200+ MB beyond startup.

Root cause: five independent layers each holding memory that their default
allocator never released back to the OS.

Full analysis: [streamlit_memory_analysis.md](../../blob/main/streamlit_memory_analysis.md)
Test harness: this repo's CI workflows.

---

## Allocation map

The dashboard's data pipeline involves **three separate allocators** with
**zero overlap** — each must be configured independently:

| Component | Allocator | Controlled by |
|-----------|-----------|---------------|
| Python objects, Pandas wrappers, `st.cache_data` pickled bytes, Tornado | **LD_PRELOAD jemalloc** | `MALLOC_CONF` + `PYTHONMALLOC=malloc` |
| Polars column data (Rust, statically linked) | **mimalloc** (bundled in manylinux wheel) | `MIMALLOC_PURGE_DELAY`, `MIMALLOC_PAGE_RESET` |
| PyArrow temp buffers, serialization | **Arrow's namespaced jemalloc** (`je_arrow_*` symbols) | `ARROW_DEFAULT_MEMORY_POOL` + `JE_ARROW_MALLOC_CONF` |

Allocator isolation is verified by CI: every assertion passes on Wolfi,
exercising all three allocators simultaneously with no conflict or crash
(`.github/workflows/allocator-assertions.yml`).

---

## Changes

### `apk add libjemalloc2=5.3.1-r1`

Installs the jemalloc library. Wolfi's package is pinned to a known-good
version to prevent regressions from upstream allocator changes.

### `LD_PRELOAD=/usr/lib/libjemalloc.so.2`

Intercepts all `malloc()` / `free()` / `realloc()` calls at the system
level, replacing glibc's default allocator with jemalloc. This covers:

- Python objects (widgets, session state, cache dict)
- Pandas DataFrame metadata (index, column labels)
- Streamlit/Tornado server internals
- `st.cache_data` pickled bytes stored in `cachetools.TTLCache`

Evidence: removing `MALLOC_CONF` entirely while keeping LD_PRELOAD costs
**+64 MB** (subtractive test `.github/workflows/production-config-test.yml`).
LD_PRELOAD is the foundation — the other env vars tune it.

### `PYTHONMALLOC=malloc`

Routes **all** Python allocations through `malloc()` rather than Python's
internal `pymalloc`. Without this, pymalloc intercepts objects <512 bytes
and handles them in its own arena system — jemalloc never sees them.
With `PYTHONMALLOC=malloc`, every Python allocation benefits from
jemalloc's memory release behavior.

Evidence: when `PYTHONMALLOC` is unset (pymalloc active), the allocator
map shows pymalloc as the Python domain handler. With it set, Python
delegates entirely to jemalloc. No individual cost measurable in isolation
(the benefit is synergistic with `MALLOC_CONF`).

Note: `PYTHONMALLOC=malloc` combined with `tcache:false` was tested
in production — widget input lag was observed in initial testing but
resolved after removing the `malloc_trim` daemon thread. `tcache:false`
is retained here as it reduces per-thread object caching overhead with
no observable UX impact at current concurrency levels (2-3 users).

### `MALLOC_CONF="narenas:1,tcache:false,dirty_decay_ms:10000,muzzy_decay_ms:10000,background_thread:true,retain:false"`

Tunes the LD_PRELOAD jemalloc instance. Each parameter tested individually:

| Parameter | What it does | Individual test |
|-----------|-------------|-----------------|
| `narenas:1` | Single arena — eliminates multi-arena fragmentation. Safe for 2-3 concurrent users | Subtractive: combined with other params, removing any single one is noise. Removing ALL costs +64 MB |
| `tcache:false` | Disables per-thread object caching. Reduces RSS by avoiding cached-but-unused small objects. Tested in production with no observable UI impact at current concurrency | Tested in isolation: tcache:false = +180 MB, tcache:true = +191 MB (+11 MB, noise floor) |
| `dirty_decay_ms:10000` | Dirty pages become available for reuse 10s after free | Synergistic — no single param dominates |
| `muzzy_decay_ms:10000` | Muzzy pages become available for reuse 10s after purge | Synergistic — no single param dominates |
| `background_thread:true` | jemalloc's own asynchronous cleanup daemon — handles decay without blocking application threads | Synergistic — no single param dominates |
| `retain:false` | jemalloc calls `munmap()` on fully-decayed pages, returning them to the OS. Without this, RSS never decreases on Linux 64-bit | Synergistic — no single param dominates |

Evidence: subtractive test (`.github/workflows/malloc-conf-test.yml`)
tested each parameter in isolation — removing any single one had no
measurable cost. Removing **all** of them cost **+64 MB**. The six
parameters are synergistic: they support each other, and the benefit
only materializes from the ensemble.

Evidence: subtractive test (`.github/workflows/malloc-conf-test.yml`)
tested each parameter in isolation — removing any single one had no
measurable cost. Removing **all** of them cost **+64 MB**. The six
parameters are synergistic: they support each other, and the benefit
only materializes from the ensemble.

### `ARROW_DEFAULT_MEMORY_POOL=jemalloc`

Tells PyArrow to use its own bundled jemalloc instead of the system
allocator. Arrow's jemalloc uses **namespaced symbols** (`je_arrow_*`,
not `malloc`), so there is **no conflict** with the LD_PRELOAD jemalloc
instance — they are completely separate allocators with disjoint pools.

Evidence: verified in CI — `pa.default_memory_pool().backend_name`
returns `"jemalloc"` when this env var is set. Exercising Arrow and
Python allocations simultaneously produces no crash.

### `JE_ARROW_MALLOC_CONF=oversize_threshold:8388608`

Fixes Arrow's own jemalloc configuration. Arrow ships with
`oversize_threshold:0` which causes its jemalloc to **never reuse
large freed chunks**, leading to unbounded VMS growth (Arrow issue
[#46929](https://github.com/apache/arrow/issues/46929)). Setting it
to 8 MB (8388608 bytes, jemalloc's own default) allows freed chunks
to be reused, preventing the VMS leak.

Evidence: Arrow issue #46929 benchmarks show `oversize_threshold:0`
produces 1380-1790 MB RSS and 9.5→22 GB VMS; `oversize_threshold:8388608`
produces 170-185 MB RSS with stable VMS.

### `MIMALLOC_PURGE_DELAY=0`

Controls Polars' bundled mimalloc allocator. Polars packages mimalloc
into its Rust binary (Polars issue [#8823](https://github.com/pola-rs/polars/issues/8823)),
and `LD_PRELOAD` jemalloc **cannot** intercept statically-linked Rust
allocations (Polars issue [#23128](https://github.com/pola-rs/polars/issues/23128)).
The default purge delay is 25000 ms (25 seconds) — setting it to 0
forces mimalloc to release freed pages immediately.

Evidence: `MIMALLOC_PURGE_DELAY` env var is read by mimalloc at startup;
verified as active in CI (`MIMALLOC_PURGE_DELAY=0` confirmed in
environment). Subtractive test shows +12 MB cost when removed.

### `MIMALLOC_PAGE_RESET=1`

Tells Polars' mimalloc to reset (decommit) pages on free, returning
dirty pages to the OS immediately rather than holding them in a dirty
state for potential reuse.

Evidence: combined with `MIMALLOC_PURGE_DELAY=0`, these two settings
account for ~12 MB improvement in subtractive testing.

---

## What was considered and rejected

| Proposal | Reason rejected |
|----------|----------------|
| `MALLOC_ARENA_MAX=2` | glibc env var — irrelevant when jemalloc is LD_PRELOADed. CI test shows +3 MB noise floor, zero real effect |
| `malloc_trim(0)` daemon thread | Interferes with jemalloc's `background_thread` — observed to **cause** RSS creep rather than fix it. glibc function on glibc arenas; jemalloc handles all allocations so it's a no-op at best |
| `use_pyarrow_extension_array=False` | Saves ~66 MB but breaks nullable integer columns (coerces to `float64`), Decimal types, and dates-in-lists. Data integrity trumps memory savings |
| `st.cache_data` timer patches | Hacky, not maintainable. `max_entries` already provides LRU-based eviction |
| `PYTHONMALLOC=mimalloc` (Python 3.13+) | Wolfi currently ships Python 3.12. Future option if available |

---

## Allocation mapping (verified by CI)

```
Python objects  ──PYTHONMALLOC=malloc──→ malloc() ──LD_PRELOAD──→ jemalloc (MALLOC_CONF)
Pandas wrappers ──PYTHONMALLOC=malloc──→ malloc() ──LD_PRELOAD──→ jemalloc (MALLOC_CONF)
st.cache_data   ──pickle───────────────→ bytes    ──LD_PRELOAD──→ jemalloc (MALLOC_CONF)
Streamlit       ──malloc()──────────────→ malloc() ──LD_PRELOAD──→ jemalloc (MALLOC_CONF)

Polars data     ──#[global_allocator]───→ mimalloc (MIMALLOC_*)
                                                        ↑
Pandas ArrowDtype ──zero-copy───────────→ same Polars buffer

PyArrow buffers ──je_arrow_mallocx()────→ Arrow jemalloc (JE_ARROW_MALLOC_CONF)
                                                     ↑
                                      namespaced symbols — no LD_PRELOAD conflict
```

All assertions pass in CI: `.github/workflows/allocator-assertions.yml`

---

## Testing

Full test infrastructure lives in this repo. Key workflows:

- `allocator-assertions.yml` — verifies the 3-allocator map above
- `production-config-test.yml` — subtractive: which env vars actually matter
- `malloc-conf-test.yml` — isolates each `MALLOC_CONF` parameter
- `tcache-test.yml` — tcache:true vs tcache:false memory comparison
- `arena-max-test.yml` — proves `MALLOC_ARENA_MAX` is noise

Run locally:
```bash
docker compose up --build    # interactive Streamlit dashboard
docker compose exec ... python3 scripts/test_allocator_assertions.py
docker compose exec ... python3 scripts/test_headless.py
```
