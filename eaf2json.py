#!/usr/bin/env python3
"""
EAF (ELAN) → JSON 変換スクリプト

入力: EAFファイル（text / morph / gloss / pos 層を含む）
出力: テンプレ準拠の JSON

各発話ごとに以下のフィールドを生成:
  text      : text層のテキスト（形態素境界つき表記）
  morphs    : 形態素リスト（句読点を除去）
  gloss     : グロスリスト
  pos       : 品詞リスト
  extw_id   : 拡張語ID（スペース区切りの語単位）
  w_id      : 語単位ID（スペース + = 区切りの単位）
  word_pos  : w_id 単位の品詞（先頭の非PX形態素の品詞。PXは接頭辞）
  boundary  : 境界マーカー (EAC / EOS / EQC / Q 等)
  sentence_id : 文ID（EOS でインクリメント）
  meta      : メタ情報
"""

import xml.etree.ElementTree as ET
import json
import re
import sys
import argparse
from pathlib import Path


# ---------------------------------------------------------------------------
# EAF パース
# ---------------------------------------------------------------------------

def parse_eaf(eaf_path):
    """EAF ファイルをパースし、層ごとの構造を返す。"""
    tree = ET.parse(eaf_path)
    root = tree.getroot()

    # タイムスロット
    time_slots = {}
    for ts in root.findall('.//TIME_SLOT'):
        time_slots[ts.get('TIME_SLOT_ID')] = int(ts.get('TIME_VALUE'))

    # 全層を辞書に格納
    tiers = {}
    for tier_elem in root.findall('TIER'):
        tier_id = tier_elem.get('TIER_ID')
        tier_type = tier_elem.get('LINGUISTIC_TYPE_REF')
        parent_ref = tier_elem.get('PARENT_REF')

        anns = []
        for aa in tier_elem.findall('.//ALIGNABLE_ANNOTATION'):
            anns.append({
                'id': aa.get('ANNOTATION_ID'),
                'value': (aa.find('ANNOTATION_VALUE').text or '').strip(),
                'ts1': aa.get('TIME_SLOT_REF1'),
                'ts2': aa.get('TIME_SLOT_REF2'),
                'kind': 'alignable',
            })
        for ra in tier_elem.findall('.//REF_ANNOTATION'):
            anns.append({
                'id': ra.get('ANNOTATION_ID'),
                'value': (ra.find('ANNOTATION_VALUE').text or '').strip(),
                'ref': ra.get('ANNOTATION_REF'),
                'prev': ra.get('PREVIOUS_ANNOTATION'),
                'kind': 'ref',
            })

        tiers[tier_id] = {
            'id': tier_id,
            'type': tier_type,
            'parent': parent_ref,
            'annotations': anns,
        }

    return tiers, time_slots


# ---------------------------------------------------------------------------
# 層の探索
# ---------------------------------------------------------------------------

def find_tier_by_type(tiers, type_name):
    """LINGUISTIC_TYPE_REF が type_name の層を返す。"""
    for t in tiers.values():
        if t['type'] == type_name:
            return t
    return None


def find_child_tiers(tiers, parent_id, type_name):
    """parent_id の子で type_name に一致する層を返す。"""
    for t in tiers.values():
        if t['parent'] == parent_id and t['type'] == type_name:
            return t
    return None


def find_child_tiers_by_id(tiers, parent_id, id_prefix):
    """parent_id の子で TIER_ID が id_prefix で始まる層を返す（フォールバック用）。"""
    for t in tiers.values():
        if t['parent'] == parent_id and t['id'].startswith(id_prefix):
            return t
    return None


def resolve_tiers(tiers):
    """
    morph 層を基点として text / gloss / pos 層を特定する。
    まず LINGUISTIC_TYPE_REF で探し、見つからなければ TIER_ID の先頭で判別する。
    """
    morph_tier = find_tier_by_type(tiers, 'morph')
    if morph_tier is None:
        raise ValueError("morph 層が見つかりません")

    text_tier_id = morph_tier['parent']
    text_tier = tiers.get(text_tier_id)
    if text_tier is None:
        raise ValueError(f"text 層 '{text_tier_id}' が見つかりません")

    gloss_tier = find_child_tiers(tiers, morph_tier['id'], 'gloss')
    if gloss_tier is None:
        gloss_tier = find_child_tiers_by_id(tiers, morph_tier['id'], 'gloss')

    pos_tier = find_child_tiers(tiers, morph_tier['id'], 'pos')
    if pos_tier is None:
        pos_tier = find_child_tiers_by_id(tiers, morph_tier['id'], 'pos')

    # text 層がさらに親を持つ場合（exercise_ant4.eaf 形式）
    top_tier = text_tier
    if text_tier['parent']:
        top_tier = tiers.get(text_tier['parent'], text_tier)

    return top_tier, text_tier, morph_tier, gloss_tier, pos_tier


# ---------------------------------------------------------------------------
# アノテーションの整列
# ---------------------------------------------------------------------------

def order_subdivisions(anns):
    """
    Symbolic_Subdivision のアノテーションを PREVIOUS_ANNOTATION チェーンで整列。
    """
    if not anns:
        return []

    by_id = {a['id']: a for a in anns}

    # prev を持たないもの → チェーンの先頭
    first = None
    for a in anns:
        if a.get('prev') is None:
            first = a
            break
    if first is None:
        return anns  # フォールバック

    ordered = [first]
    current_id = first['id']
    visited = {current_id}

    # prev → next の逆引き
    next_map = {}
    for a in anns:
        if a.get('prev') is not None:
            next_map[a['prev']] = a

    while current_id in next_map:
        nxt = next_map[current_id]
        if nxt['id'] in visited:
            break
        ordered.append(nxt)
        visited.add(nxt['id'])
        current_id = nxt['id']

    return ordered


def group_by_parent(anns):
    """アノテーションを ANNOTATION_REF (親ID) ごとにグループ化。"""
    groups = {}
    for a in anns:
        ref = a.get('ref')
        if ref is not None:
            groups.setdefault(ref, []).append(a)
    return groups


# ---------------------------------------------------------------------------
# テキスト行パース → extw_id / w_id 計算
# ---------------------------------------------------------------------------

def parse_text_line(text, morphs):
    """
    テキスト行から各形態素の区切り種別を推定する。

    返値: separators — 各形態素の「直前」の区切り種別リスト
        'S'  : スペース（語境界）
        '-'  : ハイフン（接辞境界、同一 extw_id・同一 w_id）
        '='  : イコール（接語境界、同一 extw_id・異なる w_id）
        '^'  : 先頭（最初の形態素）
        ''   : 区切りなし（句読点が直結など）
    """
    separators = []
    pos = 0
    text_clean = text  # そのまま使う

    for i, morph in enumerate(morphs):
        if i == 0:
            # テキスト先頭：空白をスキップ
            while pos < len(text_clean) and text_clean[pos] == ' ':
                pos += 1
            separators.append('^')
        else:
            # 直前の区切り文字を読む
            if pos < len(text_clean):
                ch = text_clean[pos]
                if ch == ' ':
                    separators.append('S')
                    pos += 1
                    # 連続スペースのスキップ
                    while pos < len(text_clean) and text_clean[pos] == ' ':
                        pos += 1
                elif ch == '-':
                    separators.append('-')
                    pos += 1
                elif ch == '=':
                    separators.append('=')
                    pos += 1
                else:
                    # 区切りなし（句読点が直結）
                    separators.append('')
            else:
                separators.append('')

        # 形態素本体を消費
        if pos + len(morph) <= len(text_clean) and text_clean[pos:pos + len(morph)] == morph:
            pos += len(morph)
        else:
            # フォールバック: テキスト中で morph を探す
            idx = text_clean.find(morph, pos)
            if idx >= 0:
                # idx と pos の間にある文字から区切りを再判定
                gap = text_clean[pos:idx]
                if ' ' in gap:
                    separators[-1] = 'S'
                elif '=' in gap:
                    separators[-1] = '='
                elif '-' in gap:
                    separators[-1] = '-'
                pos = idx + len(morph)
            else:
                # 見つからない場合はスキップ
                pass

    return separators


def compute_ids(separators):
    """
    separators リストから extw_id と w_id を計算する。

    extw_id: スペース区切りでインクリメント
    w_id   : スペース・= 区切りでインクリメント
    """
    extw_ids = []
    w_ids = []
    ew = 0
    w = 0
    for i, sep in enumerate(separators):
        if i == 0:
            pass  # 初期値のまま
        elif sep == 'S':
            ew += 1
            w += 1
        elif sep == '=':
            w += 1
        elif sep == '-':
            pass  # 同一語内
        elif sep == '':
            pass  # 直結（句読点など）
        extw_ids.append(ew)
        w_ids.append(w)

    return extw_ids, w_ids


# ---------------------------------------------------------------------------
# 句読点の除去 & boundary 生成
# ---------------------------------------------------------------------------

# 句読点として扱うグロス
PUNCT_GLOSSES = {'EOS', 'EOA'}
# 句読点として扱う形態素値
PUNCT_MORPHS = {'.', ',', '、', '。', '?', '!', '？', '！'}
# 形態素末尾に付着しうる句読点文字
TRAILING_PUNCT_CHARS = {'.', ',', '、', '。', '?', '!', '？', '！'}
# 末尾句読点 → boundary マッピング
TRAILING_PUNCT_BOUNDARY = {
    '.': 'EOS', '。': 'EOS',
    ',': 'EAC', '、': 'EAC',
    '?': 'EOS', '？': 'EOS',
    '!': 'EOS', '！': 'EOS',
}


def strip_trailing_punct(text, morphs, glosses, poses):
    """
    形態素に付着した末尾の句読点を剥がす前処理。

    例: morph='ca.' → morph='ca', boundary_hint=EOS
        morph='agai,' → morph='agai', boundary_hint=EOS (INTJは,でもEOS扱い)
        text='nkjaan=ca.' → text='nkjaan=ca'

    Returns
    -------
    text : str (末尾句読点を除去済み)
    morphs, glosses, poses : list (変更なし or 句読点除去済み)
    boundary_hints : dict  {morph_index: boundary_type}
    """
    new_morphs = list(morphs)
    boundary_hints = {}

    for i in range(len(new_morphs)):
        m = new_morphs[i]
        if len(m) < 2:
            continue
        # 末尾の連続する句読点を剥がす（例: 'n..' → 'n', 'ca.' → 'ca'）
        stripped = m.rstrip(''.join(TRAILING_PUNCT_CHARS))
        if stripped and stripped != m:
            punct_part = m[len(stripped):]
            # 最後の句読点文字で boundary を決定
            last_punct = punct_part[-1]
            bnd = TRAILING_PUNCT_BOUNDARY.get(last_punct, 'EAC')
            # INTJ に , が付く場合は例外的に EOS 扱い
            if last_punct in (',', '、') and i < len(poses) and poses[i] == 'INTJ':
                bnd = 'EOS'
            new_morphs[i] = stripped
            boundary_hints[i] = bnd

    # テキスト行からも末尾句読点を除去
    new_text = text
    if boundary_hints:
        new_text = text.rstrip(''.join(TRAILING_PUNCT_CHARS))
        # 内部の句読点（発話途中のピリオド）も処理
        # 例: "ifɨ-tar=ca. irav=nu pama=nkai."
        # → 各 morph の位置に対応する句読点を除去
        for i, (old_m, new_m) in enumerate(zip(morphs, new_morphs)):
            if old_m != new_m:
                punct_part = old_m[len(new_m):]
                new_text = new_text.replace(old_m, new_m, 1)

    return new_text, new_morphs, glosses, poses, boundary_hints


def is_punct(morph_val, gloss_val, pos_val):
    """形態素が句読点かどうかを判定。"""
    if gloss_val in PUNCT_GLOSSES:
        return True
    if pos_val == 'PCT':
        return True
    if morph_val in PUNCT_MORPHS:
        return True
    return False


def compute_boundary_from_punct(gloss_val):
    """句読点グロスから boundary マーカーを返す。"""
    if gloss_val == 'EOA':
        return 'EAC'
    if gloss_val == 'EOS':
        return 'EOS'
    # デフォルト: 形態素値で判定
    return 'EAC'


def process_utterance(text, morphs, glosses, poses, separators, utt_index,
                      sentence_counter, boundary_hints=None):
    """
    1発話分のデータを処理し、テンプレ準拠の辞書を返す。

    Parameters
    ----------
    sentence_counter : int
        現在の文ID（呼び出し側が管理）
    boundary_hints : dict, optional
        strip_trailing_punct で検出された {morph_index: boundary_type}

    Returns
    -------
    result : dict
    new_sentence_counter : int
    """
    if boundary_hints is None:
        boundary_hints = {}
    n = len(morphs)
    extw_ids, w_ids = compute_ids(separators)

    # ------------------------------------------------------------------
    # boundary の初期化（句読点除去前）
    # ------------------------------------------------------------------
    boundary = [None] * n

    # EQC: QUOT (CJP) で extw_id グループの末尾にあるもの
    for i in range(n):
        if glosses[i] == 'QUOT' and poses[i] == 'CJP':
            # この形態素が extw_id グループの末尾かチェック
            is_last_in_extw = (i == n - 1) or (extw_ids[i + 1] != extw_ids[i])
            # または次が句読点
            if not is_last_in_extw and i + 1 < n and is_punct(morphs[i + 1], glosses[i + 1], poses[i + 1]):
                is_last_in_extw = True
            if is_last_in_extw:
                boundary[i] = 'EQC'

    # 句読点形態素を検出し、直前の形態素に boundary を付与
    punct_indices = set()
    for i in range(n):
        if is_punct(morphs[i], glosses[i], poses[i]):
            punct_indices.add(i)
            bnd = compute_boundary_from_punct(glosses[i])
            # 直前の非句読点形態素を探す
            prev_idx = i - 1
            while prev_idx >= 0 and prev_idx in punct_indices:
                prev_idx -= 1
            if prev_idx >= 0:
                # 既存の boundary と結合
                if boundary[prev_idx] is not None:
                    # EQC + EOS → EQC+EOS のような結合
                    if bnd not in boundary[prev_idx]:
                        boundary[prev_idx] = boundary[prev_idx] + '+' + bnd
                else:
                    boundary[prev_idx] = bnd

    # 形態素に付着していた句読点から boundary を付与（strip_trailing_punct の結果）
    for i, bnd in boundary_hints.items():
        if i < n and i not in punct_indices:
            if boundary[i] is not None:
                if bnd not in boundary[i]:
                    boundary[i] = boundary[i] + '+' + bnd
            else:
                boundary[i] = bnd

    # ------------------------------------------------------------------
    # 句読点を除去して各リストをフィルタ
    # ------------------------------------------------------------------
    keep = [i for i in range(n) if i not in punct_indices]

    out_morphs = [morphs[i] for i in keep]
    out_glosses = [glosses[i] for i in keep]
    out_poses = [poses[i] for i in keep]
    out_boundary = [boundary[i] for i in keep]
    out_extw_ids = [extw_ids[i] for i in keep]
    out_w_ids = [w_ids[i] for i in keep]

    # extw_id / w_id を 0 から振り直す
    if out_extw_ids:
        out_extw_ids = renumber(out_extw_ids)
        out_w_ids = renumber(out_w_ids)

    # ------------------------------------------------------------------
    # word_pos: w_id グループごとの代表品詞
    #   PX（接頭辞: NPX, VPX 等）は後続の非PX形態素の品詞に揃える
    # ------------------------------------------------------------------
    # まず w_id ごとにインデックスを集める
    w_id_groups = {}
    for i, wid in enumerate(out_w_ids):
        w_id_groups.setdefault(wid, []).append(i)

    # 各グループの代表品詞を決定
    w_head_pos = {}
    for wid, indices in w_id_groups.items():
        # 最初の非PX形態素の品詞を使う
        head_pos = None
        for idx in indices:
            if not out_poses[idx].endswith('PX'):
                head_pos = out_poses[idx]
                break
        if head_pos is None:
            # 全てPXの場合はフォールバック
            head_pos = out_poses[indices[0]]
        w_head_pos[wid] = head_pos

    out_word_pos = [w_head_pos[wid] for wid in out_w_ids]

    # ------------------------------------------------------------------
    # sentence_id: EOS boundary でインクリメント
    # ------------------------------------------------------------------
    out_sentence_ids = []
    cur_sent = sentence_counter
    for i in range(len(out_morphs)):
        out_sentence_ids.append(cur_sent)
        if out_boundary[i] is not None and 'EOS' in out_boundary[i]:
            cur_sent += 1

    new_sentence_counter = cur_sent

    # ------------------------------------------------------------------
    # meta
    # ------------------------------------------------------------------
    orig_len = n
    clean_len = len(out_morphs)
    meta = {
        'utt_index': utt_index,
        'boundary_fallback': False,
        'orig_len': orig_len,
        'clean_len': clean_len,
    }

    result = {
        'text': text,
        'morphs': out_morphs,
        'gloss': out_glosses,
        'pos': out_poses,
        'extw_id': out_extw_ids,
        'w_id': out_w_ids,
        'word_pos': out_word_pos,
        'boundary': out_boundary,
        'sentence_id': out_sentence_ids,
        'meta': meta,
    }

    return result, new_sentence_counter


def renumber(ids):
    """ID リストを出現順に 0 から振り直す。"""
    mapping = {}
    counter = 0
    result = []
    for v in ids:
        if v not in mapping:
            mapping[v] = counter
            counter += 1
        result.append(mapping[v])
    return result


# ---------------------------------------------------------------------------
# メイン変換
# ---------------------------------------------------------------------------

def convert_eaf_to_json(eaf_path):
    """EAF ファイルを読み込み、テンプレ準拠の辞書リストを返す。"""
    tiers, time_slots = parse_eaf(eaf_path)
    top_tier, text_tier, morph_tier, gloss_tier, pos_tier = resolve_tiers(tiers)

    # アノテーションをグループ化
    morph_groups = group_by_parent(morph_tier['annotations'])
    gloss_lookup = {}
    if gloss_tier:
        for a in gloss_tier['annotations']:
            gloss_lookup[a['ref']] = a['value']
    pos_lookup = {}
    if pos_tier:
        for a in pos_tier['annotations']:
            pos_lookup[a['ref']] = a['value']

    # text 層のアノテーション一覧（時間順）
    if text_tier['annotations'] and text_tier['annotations'][0].get('kind') == 'alignable':
        text_anns = sorted(
            text_tier['annotations'],
            key=lambda a: time_slots.get(a.get('ts1'), 0),
        )
    else:
        # ref ベース → 親層の時間順で並べる
        top_order = {}
        for i, a in enumerate(sorted(
            top_tier['annotations'],
            key=lambda a: time_slots.get(a.get('ts1'), 0),
        )):
            top_order[a['id']] = i

        text_anns = sorted(
            text_tier['annotations'],
            key=lambda a: top_order.get(a.get('ref'), 0),
        )

    # 発話ごとに変換
    results = []
    sentence_counter = 0

    for utt_idx, text_ann in enumerate(text_anns):
        text_ann_id = text_ann['id']
        text_value = text_ann['value']

        # morph が紐づかない発話はスキップ
        if text_ann_id not in morph_groups:
            continue

        ordered = order_subdivisions(morph_groups[text_ann_id])
        morphs = [a['value'] for a in ordered]
        morph_ids = [a['id'] for a in ordered]
        glosses = [gloss_lookup.get(mid, '') for mid in morph_ids]
        poses = [pos_lookup.get(mid, '') for mid in morph_ids]

        # 形態素に付着した句読点を剥がす前処理
        text_clean, morphs, glosses, poses, boundary_hints = \
            strip_trailing_punct(text_value, morphs, glosses, poses)

        # テキスト行をパースして区切り情報を得る
        separators = parse_text_line(text_clean, morphs)

        result, sentence_counter = process_utterance(
            text_value, morphs, glosses, poses, separators,
            utt_idx, sentence_counter, boundary_hints,
        )
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='EAF (ELAN) → JSON 変換スクリプト',
    )
    parser.add_argument('input', help='入力 EAF ファイルパス')
    parser.add_argument(
        '-o', '--output',
        help='出力 JSON ファイルパス（省略時は入力ファイル名.json）',
    )
    parser.add_argument(
        '--indent', type=int, default=2,
        help='JSON インデント幅（デフォルト: 2）',
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_suffix('.json')

    results = convert_eaf_to_json(input_path)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=args.indent)

    print(f'変換完了: {len(results)} 発話 → {output_path}')


if __name__ == '__main__':
    main()
