# eaf2json

ELAN (EAF) ファイルを、形態素・グロス・品詞・語境界情報を含む構造化 JSON に変換するスクリプト。

## 概要

言語ドキュメンテーション用の [ELAN](https://archive.mpi.nl/tla/elan) で作成した EAF ファイルから、text / morph / gloss / pos の各層を読み取り、発話（utterance）単位でフラットな JSON 配列に変換する。

テキスト行の形態素境界表記（スペース・`-`・`=`）から語単位の ID（`extw_id`, `w_id`）を自動算出し、句読点の処理・文境界の検出・品詞の集約（`word_pos`）も行う。

## 必要環境

- Python 3.8+
- 外部ライブラリ不要（標準ライブラリのみ）

## ファイル構成

```
eaf2json.py          # メインスクリプト（CLI / import 両対応）
run_eaf2json.ipynb   # Jupyter Lab 用ノートブック
```

## 使い方

### コマンドラインから実行

```bash
# 基本（出力は入力と同名の .json）
python eaf2json.py input.eaf

# 出力先を指定
python eaf2json.py input.eaf -o output.json

# インデント幅を変更
python eaf2json.py input.eaf --indent 4
```

### Jupyter Lab から実行

`eaf2json.py` と `run_eaf2json.ipynb` を同じフォルダに置き、ノートブックを開いてセルを上から順に実行する。EAF ファイルのパスは実行セル内の `EAF_PATH` を書き換える。

```python
from eaf2json import convert_eaf_to_json
from pathlib import Path
import json

results = convert_eaf_to_json("./input.eaf")

with open("output.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
```

### バッチ変換

ノートブックの最終セル、またはシェルのワンライナーで一括処理できる。

```bash
for f in *.eaf; do python eaf2json.py "$f"; done
```

## 入力 EAF の想定構造

以下の層構成を想定する。層の検出は `LINGUISTIC_TYPE_REF` で行い、見つからない場合は `TIER_ID` の先頭文字列でフォールバックする。

```
text (TIME_ALIGNABLE)        ← 形態素境界つきテキスト
├── morph (Symbolic_Subdivision)  ← 個別形態素
│   ├── gloss (Symbolic_Association)
│   └── pos   (Symbolic_Association)
└── trans (Symbolic_Association)  ← 翻訳（任意）
```

`text` 層の上にさらに `text0` のような親層がある構成（2層構造）にも対応する。

## 出力 JSON の構造

出力は発話ごとのオブジェクトを並べた JSON 配列。各オブジェクトのフィールドは以下のとおり。

```jsonc
{
  "text": "hai zjaa o-hanasi s-i-mas-u.",   // text層の原文
  "morphs":   ["hai","zjaa","o","hanasi","s","i","mas","u"],
  "gloss":    ["はい","じゃあ","SFN","話","する","THM","POL","NPST"],
  "pos":      ["INTJ","INTJ","NPX","N","V","VX","VX","VX"],
  "extw_id":  [0, 1, 2, 2, 3, 3, 3, 3],
  "w_id":     [0, 1, 2, 2, 3, 3, 3, 3],
  "word_pos": ["INTJ","INTJ","N","N","V","V","V","V"],
  "boundary": [null, null, null, null, null, null, null, "EOS"],
  "sentence_id": [0, 0, 0, 0, 0, 0, 0, 0],
  "meta": {
    "utt_index": 0,
    "boundary_fallback": false,
    "orig_len": 9,
    "clean_len": 8
  }
}
```

### フィールド詳細

| フィールド | 説明 |
|---|---|
| `text` | text 層の原テキスト（形態素境界つき表記） |
| `morphs` | 形態素のリスト。句読点は除去済み |
| `gloss` | 各形態素のグロス |
| `pos` | 各形態素の品詞タグ |
| `extw_id` | 拡張語 ID。スペース区切りでインクリメント |
| `w_id` | 語 ID。スペースと `=` でインクリメント |
| `word_pos` | `w_id` グループの代表品詞 |
| `boundary` | 境界マーカー（`EOS` / `EAC` / `EQC` 等） |
| `sentence_id` | 文 ID。`EOS` でインクリメント。発話をまたいで連番 |
| `meta` | メタ情報（発話インデックス、句読点除去前後の長さ等） |

## ID 算出ルール

テキスト行の区切り文字から `extw_id` と `w_id` を決定する。

| 区切り | 例 | extw_id | w_id |
|---|---|---|---|
| スペース | `ano jookan` | インクリメント | インクリメント |
| `-`（ハイフン） | `sun-de` | そのまま | そのまま |
| `=`（イコール） | `jookan=ni` | そのまま | インクリメント |

例: `ano jookan=ni sun-de=masi-te`

```
morph:    ano  jookan  ni  sun  de  masi  te
extw_id:   0     1     1    2   2    2    2
w_id:      0     1     2    3   3    4    4
```

## word_pos の決定ルール

`w_id` グループ内の最初の非 PX（接頭辞でない）形態素の品詞（語彙的な品詞、語根類）を代表品詞とする。

| グループ内 POS | word_pos | 理由 |
|---|---|---|
| V - VX - VX - VX | V | 先頭 V が非 PX → そのまま |
| NPX - N | N | NPX をスキップ → 次の N を採用 |
| VPX - V - VX | V | VPX をスキップ → 次の V を採用 |

## 句読点・boundary の処理

### 独立句読点型（gloss が `EOS` / `EOA`、POS が `PCT` の形態素）

句読点形態素を `morphs` から除去し、直前の形態素に boundary マーカーを付与する。

- gloss `EOS` → boundary `EOS`（文末）
- gloss `EOA` → boundary `EAC`（節末）
- `QUOT` (CJP) → boundary `EQC`（引用節末）

### 付着句読点型（形態素末尾にピリオド・カンマが付く場合）

`ca.` → `ca` + boundary `EOS`、`agai,` → `agai` + boundary `EAC` のように、形態素から句読点を剥がして boundary に変換する。

- `.` `。` `?` `!` → `EOS`
- `,` `、` → `EAC`
- 例外: POS が `INTJ` の形態素に `,` が付く場合は `EOS` 扱い

### sentence_id

`EOS` boundary が現れるたびにグローバルカウンタをインクリメントする。`EAC` ではインクリメントしない。1 発話内に複数の文が含まれる場合も正しく分割される。

## 対応している EAF バリエーション

| パターン | 説明 |
|---|---|
| text 層がトップレベル | `text` が直接 `TIME_ALIGNABLE` |
| text0 → text の2層構造 | `text0`（時間整列）の子に `text`（ref） |
| pos 層の type が `"pos"` | `LINGUISTIC_TYPE_REF` で検出 |
| pos 層の type が `"translation"` 等 | `TIER_ID` が `"pos"` で始まるかでフォールバック検出 |
| 句読点が独立形態素 | gloss `EOS` / `EOA`、POS `PCT` で判定 |
| 句読点が形態素に付着 | 末尾の `.` `,` 等を自動検出・剥離 |

## ライセンス

MIT
