# Chinese-English Dictionary Database

Chinese name: 中英词典词库

This repository packages a compact Chinese-English dictionary database and the small local query service used to serve it.

## Contents

- `release/chinese-english-dictionary-database_20260622.zip`
  - `dictionary_compact.db`
  - `dictionary_package_manifest.json`
  - source manifests
- `dictionary_engine/`
  - SQLite build, query, HSK, suggestion, and duplicate-cleanup tools
- `dictionary_frontend.html`
  - local browser UI for querying the API at `http://127.0.0.1:8765`
- `docs/`
  - build and runtime notes

## Database Summary

- SQLite database size before ZIP: 640,106,496 bytes
- ZIP size: 244,610,855 bytes
- ZIP SHA256: `7da60c30a2bb0edd738a4c1ce156be304eba3e802f2ba17bed8318daecb719e5`
- DB SHA256: `6369d6e8ad498cc50582bdc26827e37351df1fd741ad52907683c3769b408cf1`
- Lexemes: 274,061
- Example sentences after duplicate cleanup: 802,920

Duplicate examples are removed when the Chinese and English text are exactly equal after normalization, or equal after stripping only terminal punctuation from both sides.

## Run Locally

Unzip the release package so `dictionary_compact.db` is available, then run:

```powershell
python -m dictionary_engine.server `
  --db dictionary_compact.db `
  --host 127.0.0.1 `
  --port 8765 `
  --pool-size 2 `
  --cache-mb 64
```

Open `dictionary_frontend.html` in a browser and query words such as `学习`, `A股`, `三彩`, or English suggestions such as `study`.

## Large File Note

The release ZIP is larger than GitHub's normal 100MB file limit. This repository tracks `release/*.zip` with Git LFS.
