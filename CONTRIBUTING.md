# Contributing

Thanks for helping. diskscout touches people's files, so the bar for safety is higher than the bar for features.

## Ground rules

- **Standard library only.** No runtime dependencies.
- **Scanners never modify anything.** Anything that changes the disk belongs in `diskscout/apply.py`, runs only with `--yes`, and goes through `recheck()` first.
- **When unsure, recommend review.** A finding that's wrongly listed as "needs review" costs a click. One that's wrongly recommended costs someone's work.
- **Every rule ships with a test**, including a test that the unsafe case is *not* recommended.

## Setup

```sh
git clone https://github.com/brian-childress/diskscout && cd diskscout
python3 -m unittest discover -s tests -v
```

## Common changes

| Change | Where |
|---|---|
| New cache location or cleanup command | `KNOWN` in `diskscout/scanners/caches.py` |
| New dependency/build folder type | `dep_kind()` in `diskscout/inventory.py` |
| App that must be closed before its cache is emptied | `APP_PROCS` in `diskscout/apply.py` |
| New scanner | `diskscout/scanners/<name>.py` with `scan(ctx) -> list[Finding]`, registered in `cli.SCANNERS` |

## Pull requests

- Keep PRs focused and describe the real-world case that motivated the change.
- Never commit scan output. Reports contain local paths and are git-ignored.
- CI must pass on macOS and Linux.
