# Raw Dataset Sources

Downloaded for the Chinese dictionary database prototype. Keep these files as immutable raw inputs; put parsed or cleaned output under a separate directory later.

Note: CC-CEDICT does not contain HSK levels. HSK levels were downloaded as supplemental datasets so they can be joined to CC-CEDICT entries by simplified/traditional form and pinyin.

## CC-CEDICT / MDBG

| Local file | Bytes | SHA256 | Source URL | Intended use |
| --- | ---: | --- | --- | --- |
| `mdbg_cc_cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz` | 3977444 | `DF55189D07535ED8EC7115DD5FF9C62EC6375AB1D978DC1ED506903E51EE610A` | https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz | Dictionary headwords, traditional/simplified forms, pinyin, English definitions. |

Observed sample count: 125045 dictionary entries.

## Tatoeba

| Local file | Bytes | SHA256 | Source URL | Intended use |
| --- | ---: | --- | --- | --- |
| `tatoeba/cmn_sentences.tsv.bz2` | 1229022 | `E042C1A85D1BF42EF8B68BDBD1F42911C4088183321E3BE41C7D9C0020478B7D` | https://downloads.tatoeba.org/exports/per_language/cmn/cmn_sentences.tsv.bz2 | Mandarin example sentences. |
| `tatoeba/cmn_sentences_detailed.tsv.bz2` | 1678994 | `1C1C8FBC88F11C0AFF0D87AB638D2A59C539E3F3289425919100A8D92C020695` | https://downloads.tatoeba.org/exports/per_language/cmn/cmn_sentences_detailed.tsv.bz2 | Mandarin sentences plus owner and timestamps. |
| `tatoeba/cmn_transcriptions.tsv.bz2` | 2572525 | `1E1DA5E229F66EC69CB3B18A7E3C48B70F265A134346F53EE7AC5F877F0FA9FD` | https://downloads.tatoeba.org/exports/per_language/cmn/cmn_transcriptions.tsv.bz2 | Mandarin romanization/transcription data. |
| `tatoeba/cmn-eng_links.tsv.bz2` | 474740 | `3E23A3187464C41D76646C1CC29F65080DFD04994FB5390A9B30341BF44D5B60` | https://downloads.tatoeba.org/exports/per_language/cmn/cmn-eng_links.tsv.bz2 | Links between Mandarin and English sentence ids. |
| `tatoeba/cmn_tags.tsv.bz2` | 9889 | `3448D372464B99E71E4BC4A830E0E4A3885FD80D986AF35B652D514D387F1A34` | https://downloads.tatoeba.org/exports/per_language/cmn/cmn_tags.tsv.bz2 | Optional tags for sentence filtering. |
| `tatoeba/eng_sentences.tsv.bz2` | 24757457 | `A69F8D9420EC48D142D522A2E1CCB071918BD53A722DC4997C6D92B5086D8D97` | https://downloads.tatoeba.org/exports/per_language/eng/eng_sentences.tsv.bz2 | English translations for linked Mandarin examples. |

Observed sample counts: 87264 Mandarin sentences, 77181 Mandarin-English links, 2029733 English sentences.

## OpenSubtitles / OPUS

| Local file | Bytes | SHA256 | Source URL | Intended use |
| --- | ---: | --- | --- | --- |
| `opensubtitles_opus/OpenSubtitles-v2018-en-zh_cn-moses.zip` | 361900182 | `869CA02EE7DC33C0F6A011821F505AB2BE46033CAB33C4A0B8DFC8063393E701` | https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2018/moses/en-zh_cn.txt.zip | Everyday subtitle-style sentence pairs, English-Chinese aligned Moses format. |

Zip entries:

| Entry | Uncompressed bytes | Compressed bytes |
| --- | ---: | ---: |
| `OpenSubtitles.en-zh_cn.en` | 377488449 | 141877109 |
| `OpenSubtitles.en-zh_cn.zh_cn` | 360316708 | 154537192 |
| `OpenSubtitles.en-zh_cn.ids` | 820261137 | 65484108 |
| `README` | 2455 | 1069 |

## HSK Supplements

| Local file | Bytes | SHA256 | Source URL | Intended use |
| --- | ---: | --- | --- | --- |
| `hsk/hsk30-expanded.csv` | 941057 | `62C428C1FC9C221B8AE90281B9E1C72484C7FFDF3D0476E58A4E90CFE797446F` | https://raw.githubusercontent.com/ivankra/hsk30/master/hsk30-expanded.csv | HSK 3.0 word-level mapping, expanded variants. |
| `hsk/hsk30-chars.csv` | 108366 | `8C5957108E6DAFD120BE10518A827A4D834EA3CE29ACBFEDEFF1022CD7D05280` | https://raw.githubusercontent.com/ivankra/hsk30/master/hsk30-chars.csv | HSK 3.0 character-level mapping. |
| `hsk/complete-hsk-vocabulary.json` | 9763716 | `C869A0CE353279C9333D9B42C31FC3549785E8B40673DAB57EE42BC99CD14131` | https://raw.githubusercontent.com/drkameleon/complete-hsk-vocabulary/main/complete.json | Supplemental old/new HSK levels, forms, pinyin, meanings. |

Observed sample counts: 11165 HSK 3.0 expanded rows, 3000 HSK character rows, 11470 complete HSK vocabulary items.

## hanzi-words Supplemental Headwords

Downloaded from https://github.com/zispace/hanzi-words/tree/main/dict into `hanzi_words/dict/`.

The directory has 9 UTF-8 text files plus `hanzi_words/dict/manifest.json`, which records each raw URL, byte size, and SHA256. These files provide headwords only; they are aligned to existing CC-CEDICT entries where possible, otherwise imported as empty-definition `supplement_pending` lexemes for later LLM cleaning.
