"""模型輸出驗證。

output_schemas.json 不是 JSON Schema，而是「範例形狀」，字串裡的規則決定實際檢查方式：
- 以 `（…）` 包住的全形註解先去掉，再依下列規則判斷（見 `_str_spec`）。
- 錯誤訊息的路徑從工作類型開始，例如 `THESIS.kill_criteria_status[0].status`。
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any

# 全形括號註解：例如 "數字或 null（照抄 cap_calc）"
_PAREN = re.compile(r"（[^（）]*）")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# decision_id 格式：{ticker}-{YYYYMMDD}-{n}
_DECISION_RE = re.compile(r"^([A-Z0-9.\-]+)-(\d{8})-(\d+)$")
# 選項中出現這些記號代表「自由文字」，不做列舉比對
_FREE_MARKERS = ("…", "..", "：")
# 可為 null 的欄位：模型常把 null 寫成字串，這些字串一律視同 null（否則 "null" 會被當成已具名的文字）
_NULL_STRINGS = ("null", "none", "")


def load_schemas(path: Path) -> dict:
    """讀取 output_schemas.json（UTF-8）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check(job_type: str, obj: Any, schemas: dict, ref: dict | None = None,
          expect: dict | None = None) -> tuple[Any, list[str]]:
    """檢查某工作的輸出。回傳 (正規化後的物件, 錯誤清單)；錯誤清單為空代表合格。

    正規化：把 "true"／"false" 字串轉成 bool；允許 null 的欄位把字串 "null"／"none"（及「字串或 null」的空字串）轉成 None。
    不修改傳入的物件。逐欄檢查後再跑跨欄位檢查（_CROSS），錯誤一併列出，重試一次就能全部修正。
    `ref`：前一階段的已存結果（FULL_B 給 phaseA.json），給定時跑 _NORMALIZE_REF：FULL_B 等級高於 FULL_A 結束等級
    （D29）或看價後必降未降（A3-M2C-01）→ 不報錯、把 certainty_grade 壓到上限並在 grade_notes 記原因（A3-M3D-04：
    報錯兩次會讓整鏈落成 MISSING_DATA；FullBHandler.post 再以摘要等級壓 cap_input 與訊號卡）。
    `expect`：{"ticker", "decision_date"}（引擎給 job.ticker、job.asof）；輸出有這兩個欄位時必須與工作相符
    （ticker 不分大小寫、「.」與「-」視同；M3_D1-10），避免多檔同跑時收到別檔的結果。
    """
    if job_type not in schemas or job_type.startswith("_"):
        raise ValueError(f"未知的工作類型：{job_type}")
    spec = schemas[job_type]
    # 模型偶爾多包一層 {"<工作類型>": {...}}：格式本身沒有這個鍵時拆開
    if (isinstance(obj, dict) and list(obj) == [job_type] and isinstance(obj[job_type], dict)
            and not (isinstance(spec, dict) and job_type in spec)):
        obj = obj[job_type]
    errors: list[str] = []
    out = _node(spec, obj, job_type, errors)
    if job_type in _CROSS and isinstance(out, dict):
        errors += _CROSS[job_type](out)
    if job_type in _NORMALIZE_REF and isinstance(out, dict) and isinstance(ref, dict):
        _NORMALIZE_REF[job_type](out, ref)
    if expect and isinstance(out, dict) and isinstance(spec, dict):
        errors += _check_expect(job_type, spec, out, expect)
    if job_type == "THESIS" and isinstance(out, dict):
        errors += _thesis_next_due(out, (expect or {}).get("decision_date"))
    return out, errors


def _norm_ticker(t: Any) -> str:
    return unicodedata.normalize("NFKC", str(t)).strip().upper().replace(".", "-").replace("/", "-")


def _check_expect(job_type: str, spec: dict, out: dict, expect: dict) -> list[str]:
    """輸出的 ticker／decision_date 與工作不符 → 錯誤（格式本身沒有該欄位、或值型別不對已由逐欄檢查報錯者不重複）。"""
    errs: list[str] = []
    t = expect.get("ticker")
    if t and "ticker" in spec and isinstance(out.get("ticker"), str) and _norm_ticker(out["ticker"]) != _norm_ticker(t):
        errs.append(f"{job_type}.ticker：應為本工作的 {t}，實際為 {out['ticker']!r}")
    d = expect.get("decision_date")
    v = out.get("decision_date")
    if d and "decision_date" in spec and isinstance(v, str) and _valid_date(v) and v != d:
        errs.append(f"{job_type}.decision_date：應為本工作的資料日 {d}，實際為 {v!r}")
    return errs


PREVIOUS_OUTPUT_CHARS = 20_000   # 回饋提示附的前次輸出上限（A3-M5A-02）


def error_feedback(errors: list[str], previous: str | None = None) -> str:
    """產生重試用的繁中說明：列出錯誤並要求只輸出合格 JSON。驗證重試前引擎重建沙箱（M5-D1-09），
    故另告知前次寫的檔已清除、須重寫全部輸出檔（M5-RS1-03）。
    previous：前次輸出全文（模型回覆的 result 文字）；claude 不保存 session，要求「判斷同前次」須讓它看得到前次（A3-M5A-02）。"""
    lines = ["上一次輸出未通過格式檢查，錯誤如下："]
    lines += [f"- {e}" for e in errors] or ["- （未列出具體錯誤）"]
    lines.append("請只輸出一個合格的 JSON 物件（形狀依本工作的 output schema），"
                 "列舉值必須與規定完全相同（陣列每個元素各填一個選項，不要把整串選項當成一個值），"
                 "不要用工作名稱包一層，不要加任何說明文字。")
    lines.append("只修正上列錯誤，不得改變已作出的判斷（停止點、停止碼、評等與已填數值）；"
                 "跨欄位矛盾時，依你原本的判斷補齊對應欄位（例如已停在某閘門 → stopped 改 true 並填停止碼）；"
                 "未評估的欄位維持 null，不得填 0 或補評等。")
    lines.append("注意：工作目錄已重建，前次嘗試寫入的檔案已全部清除。本工作若要求寫檔"
                 "（例如 FULL_A 的 report_A.md、gate3_answers.json，FULL_B 的報告與訊號卡等，DOSSIER 對資料包的追加），"
                 "請依原指示重新產出全部必要輸出檔（內容與判斷同前次），最後再輸出上述 JSON；只輸出 JSON 而不重寫檔案會被視為缺檔。")
    if previous and previous.strip():
        lines.append("\n## 前次輸出（未通過檢查的那一份；照它的判斷只修正上列錯誤）\n"
                     + previous[:PREVIOUS_OUTPUT_CHARS]
                     + ("\n…（以下截斷）" if len(previous) > PREVIOUS_OUTPUT_CHARS else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- 內部實作

def _node(spec: Any, val: Any, path: str, errors: list[str]) -> Any:
    """依範例值 spec 的型別分派檢查。"""
    if isinstance(spec, dict):
        return _obj(spec, val, path, errors)
    if isinstance(spec, list):
        return _lst(spec, val, path, errors)
    if isinstance(spec, bool):  # 必須先判斷 bool，因為 bool 是 int 的子類別
        return _bool(val, path, errors)
    if isinstance(spec, (int, float)):
        if _is_num(val):
            return val
        errors.append(f"{path}：應為數字（不得為 NaN／Infinity），實際為 {val!r}")
        return val
    if isinstance(spec, str):
        return _str_spec(spec, val, path, errors)
    return val


def _obj(spec: dict, val: Any, path: str, errors: list[str]) -> Any:
    """物件：必填鍵＝所有不以 _ 開頭的鍵；額外鍵允許；遞迴檢查。"""
    if not isinstance(val, dict):
        errors.append(f"{path}：應為物件")
        return val
    out = dict(val)
    for key, sub in spec.items():
        if key.startswith("_"):
            continue
        sub_path = f"{path}.{key}"
        if key not in val:
            errors.append(f"{sub_path}：缺少必填鍵")
            continue
        out[key] = _node(sub, val[key], sub_path, errors)
    return out


def _lst(spec: list, val: Any, path: str, errors: list[str]) -> Any:
    """list：空範例 → 任意 list；否則每個元素依第一個元素的規則檢查。"""
    if not isinstance(val, list):
        errors.append(f"{path}：應為陣列")
        return val
    if not spec:
        return val
    item_spec = spec[0]
    return [_node(item_spec, v, f"{path}[{i}]", errors) for i, v in enumerate(val)]


def _is_num(v: Any) -> bool:
    """有限數字（json.loads 預設接受 NaN／Infinity，寫進 DB 會變成非標準 JSON）。"""
    try:   # 超大整數（json.loads 接受到 4300 位）isfinite 會拋 OverflowError → 視為不合格、走格式重試（M3-R8-02）
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    except OverflowError:
        return False


# 「沒有」的佔位寫法（NFKC、去空白、小寫、去句尾標點後比對）：紅隊用 ["無"]／[""]／["none"] 表示沒有違規（M3-RS4-03）
_NONE_ITEMS = ("", "無", "沒有", "无", "没有", "none", "null", "nil", "n/a", "na", "-", "—", "無違規", "沒有違規")


def _is_none_item(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return unicodedata.normalize("NFKC", v).strip().lower().rstrip("。.!！").strip() in _NONE_ITEMS
    if isinstance(v, dict):   # number_errors 一列：每欄都是空／佔位 → 不算一筆（schema 範例本身就是一列空字串）
        return all(_is_none_item(x) for x in v.values())
    return False


def real_items(v: Any) -> list:
    """陣列中扣掉空白與「無／none／n/a…」佔位後的實際項目；非陣列當成一個元素（不因型別錯放行）。"""
    return [x for x in (v if isinstance(v, list) else [v]) if not _is_none_item(x)]


def _is_null(v: Any, *, empty: bool = False) -> bool:
    """None，或字串 "null"／"none"（忽略大小寫與前後空白）；empty=True 時空字串也算。"""
    if v is None:
        return True
    if not isinstance(v, str):
        return False
    t = v.strip().lower()
    return t in _NULL_STRINGS and (empty or t != "")


def _bool(val: Any, path: str, errors: list[str]) -> Any:
    """bool 欄位：接受 bool；字串 "true"／"false"（忽略大小寫與前後空白）正規化成 bool。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str) and val.strip().lower() in ("true", "false"):
        return val.strip().lower() == "true"
    errors.append(f"{path}：應為 true 或 false，實際為 {val!r}")
    return val


def _valid_date(s: str) -> bool:
    if not _DATE_RE.match(s):
        return False
    try:
        _dt.date.fromisoformat(s)
    except ValueError:  # 例如 2026-02-30
        return False
    return True


_RANGE = re.compile(r"^([A-Z]+)(\d+)\.\.\1?(\d+)$")


def _expand_ranges(options: list[str]) -> list[str]:
    """選項 "G7..G11" 展開成 G7、G8、…、G11（嚴格列舉）；其他選項不動。"""
    out: list[str] = []
    for o in options:
        m = _RANGE.match(o)
        if m and int(m.group(2)) <= int(m.group(3)):
            out += [f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        else:
            out.append(o)
    return out


def _str_spec(spec: str, val: Any, path: str, errors: list[str]) -> Any:
    """字串範例：依 contract 的 7 條規則（順序很重要）判斷。"""
    base = _PAREN.sub("", spec).strip()

    # 1. bool（"true | false | null" 開頭＝三值，null 合法，例如 incr_roic_gt_rd，設計決策 D97）
    if base.startswith("true | false | null"):
        if _is_null(val):
            return None
        return _bool(val, path, errors)
    if base.startswith("true | false"):
        return _bool(val, path, errors)

    # 2. 數字
    if base == "數字或 null":
        if _is_null(val):
            return None
        if _is_num(val):
            return val
        errors.append(f"{path}：應為數字或 null（不得為 NaN／Infinity），實際為 {val!r}")
        return val
    if base == "小數或 0":   # 目前無 schema 使用（A3-M3A-05），保留相容
        if _is_num(val):
            return val
        errors.append(f"{path}：應為數字（不得為 NaN／Infinity），實際為 {val!r}")
        return val

    # 3. 日期
    if base in ("YYYY-MM-DD", "YYYY-MM-DD 或 null"):
        if base.endswith("或 null") and _is_null(val):
            return None
        if isinstance(val, str) and _valid_date(val):
            return val
        errors.append(f"{path}：應為 YYYY-MM-DD 的有效日期（{base}），實際為 {val!r}")
        return val

    # 4. 字串或 null
    if base == "字串或 null":
        if _is_null(val, empty=True):
            return None
        if isinstance(val, str):
            return val
        errors.append(f"{path}：應為字串或 null，實際為 {val!r}")
        return val

    # 5. decision_id 格式：目前無 schema 使用（decision_id 改為「字串或 null」、框架另編；A3-M3A-05），保留相容
    if "{YYYYMMDD}" in base:
        m = _DECISION_RE.match(val) if isinstance(val, str) else None
        if m and _valid_date(f"{m.group(2)[:4]}-{m.group(2)[4:6]}-{m.group(2)[6:]}"):
            return val
        errors.append(f"{path}：應為 {{ticker}}-YYYYMMDD-n（日期須有效），實際為 {val!r}")
        return val

    # 6. 列舉或自由文字
    if "|" in base:
        options = _expand_ranges([o.strip() for o in base.split("|")])
        has_null = "null" in options
        if has_null and _is_null(val):  # 列舉允許 null 時，字串 "null"／"none" 視同 null
            return None
        if any(mark in o for o in options for mark in _FREE_MARKERS):
            # 含 … 等記號的選項：自由字串
            if isinstance(val, str):
                return val
            errors.append(f"{path}：應為文字，實際為 {val!r}")
            return val
        allowed = [o for o in options if o != "null"]
        if isinstance(val, str) and val in allowed:
            return val
        errors.append(f"{path}：值 {val!r} 不在允許選項內（{' | '.join(options)}）")
        return val

    # 7. 其他：任意字串
    if isinstance(val, str):
        return val
    errors.append(f"{path}：應為字串，實際為 {val!r}")
    return val


# ---------------------------------------------------------------- 跨欄位檢查（_CROSS）
# 只放「確定的矛盾」與規則明定的筆數；錯誤走既有的重試 → 不合格流程。輸入是逐欄正規化後的物件，
# 但逐欄可能已有錯（型別不對），所以這裡對型別不做假設。

# rulebook 規則編號：P-26a、SEL-B3、SEL-076b、RISK-12.1 等（SKILL 優先序段列的前綴）
# 前綴＝rule_index／rulebook 實際使用者（含 06 的 MKT-、08 的 EVID-、07 PROC-061 表的 CR-）；數字後可帶一個大寫尾碼（MKT-06B）
_RULE_ID = re.compile(r"^(?:P|SEL|VAL|RISK|POS|SELL|PROC|NEVER|CORE|EVID|MKT|CR)-[A-Z]?\d+[A-Z]?(?:\.\d+)?[a-z]?$")


def stop_rule_ids(o: dict) -> list[str] | None:
    """FULL_A 停止時的規則編號（SKILL §4 NO_ACTION 必填）：非空字串陣列且每個都照 rulebook 格式 → 去空白後的清單；
    否則 None。漏填不當作格式錯（否則退回後仍缺會被保守處理成 MISSING_DATA，違 D1-04）：處理器退回一次，再缺照停止結論
    回寫、記警示（RM2-9）。"""
    rids = o.get("rule_ids") if isinstance(o, dict) else None
    if isinstance(rids, list) and rids and all(isinstance(r, str) and _RULE_ID.match(r.strip()) for r in rids):
        return [r.strip() for r in rids]
    return None


def _len(v: Any) -> int:
    return len(v) if isinstance(v, list) else 0


def _cross_full_a(o: dict) -> list[str]:
    errs: list[str] = []
    gate, code = o.get("stopped_at_gate"), o.get("reason_code")
    if o.get("stopped") is True:
        if gate is None or code is None:
            errs.append("FULL_A：stopped=true 時 stopped_at_gate 與 reason_code 都必須填（不得為 null）")
        return errs   # 停止時的 rule_ids 不在這裡擋（漏填不得落成 MISSING_DATA）：由 FULL_A 處理器退回一次（stop_rule_ids，RM2-9）
    if gate is not None or code is not None:
        errs.append("FULL_A：stopped=false 時 stopped_at_gate 與 reason_code 必須都是 null"
                    "（停在某閘門就要 stopped=true）")
    rows = o.get("downgrades") if isinstance(o.get("downgrades"), list) else []
    for i, r in enumerate(rows):
        if isinstance(r, dict) and _driver(r) is None:   # A3-M3A-04：空白 driver_id 讓 D18 去重失效
            errs.append(f"FULL_A.downgrades[{i}].driver_id：必填非空字串（獨立驅動因素 id，D18）；沒有降級就不要列這一列")
        mg = r.get("max_grade") if isinstance(r, dict) else None
        if not _is_null(mg, empty=True) and _max_grade(r) is None:   # A3-M2B-08：上限型降級
            errs.append(f"FULL_A.downgrades[{i}].max_grade：選填 A | B | C | D 或 null（上限型規則的等級上限），實際為 {mg!r}")
    dup = _dup_drivers(rows)
    if dup:
        errs.append(f"FULL_A.downgrades：同一 driver_id 只能降一次（設計決策 D18），重複：{dup}")
    errs += _holding_stop_errors(o.get("holding_stop"))
    td = o.get("thesis_draft") if isinstance(o.get("thesis_draft"), dict) else {}
    n_ka, n_kc = _len(td.get("key_assumptions")), _len(td.get("kill_criteria"))
    if not 3 <= n_ka <= 5:
        errs.append(f"FULL_A.thesis_draft.key_assumptions：未停止時須 3–5 條，實際 {n_ka} 條")
    if not 2 <= n_kc <= 4:
        errs.append(f"FULL_A.thesis_draft.kill_criteria：未停止時須 2–4 條（SKILL G10），實際 {n_kc} 條")
    dd = o.get("decision_date")
    for i, k in enumerate(td.get("kill_criteria") if isinstance(td.get("kill_criteria"), list) else []):
        due = k.get("due") if isinstance(k, dict) else None
        if isinstance(dd, str) and _valid_date(dd) and isinstance(due, str) and _valid_date(due) and due <= dd:
            # A3-M3C-05：review_due 只在 上次檢查 < due ≤ 決策日 時觸發，過去日期永不觸發
            errs.append(f"FULL_A.thesis_draft.kill_criteria[{i}].due：須晚於決策日 {dd}（過去或當天的期限永遠不會觸發檢查），"
                        f"實際為 {due!r}")
    return errs


# 持股分支 holding_stop（D30）：gate／reason_code 的合法值（A3-M3B-04、A3-M3C-04）
HOLDING_STOP_GATES = ("G0", "G1", "G2", "G3", "4A", "G4", "G5", "G6", "產業")
HOLDING_STOP_CODES = ("MISSING_DATA", "TOO_HARD", "REJECT", "NOT_THIS_SKILL")


def _holding_stop_errors(hs: Any) -> list[str]:
    """holding_stop 只能是 null（含 "null" 字串、空物件）或 {gate, reason_code, reason}：字串等其他型別在 deep.holding_stop
    會被當成「全部通過」，持股可出 BUY／ADD，所以這裡擋下（A3-M3B-04）。"""
    if _is_null(hs, empty=True) or (isinstance(hs, dict) and all(_is_none_item(v) for v in hs.values())):
        return []
    if not isinstance(hs, dict):
        return [f"FULL_A.holding_stop：應為 null 或物件 {{gate, reason_code, reason}}（D30），實際為 {hs!r}"]
    errs = []
    if hs.get("gate") not in HOLDING_STOP_GATES:
        errs.append(f"FULL_A.holding_stop.gate：值 {hs.get('gate')!r} 不在允許選項內（{' | '.join(HOLDING_STOP_GATES)}）")
    if hs.get("reason_code") not in HOLDING_STOP_CODES:
        errs.append(f"FULL_A.holding_stop.reason_code：值 {hs.get('reason_code')!r} 不在允許選項內"
                    f"（{' | '.join(HOLDING_STOP_CODES)}）")
    return errs


def _thesis_next_due(o: dict, asof: Any) -> list[str]:
    """THESIS kill_criteria_status[].next_due（選填）：非 null 時須為有效日期且晚於決策日（A3-M3C-05、A3-M3B-08）。"""
    errs: list[str] = []
    rows = o.get("kill_criteria_status") if isinstance(o.get("kill_criteria_status"), list) else []
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or "next_due" not in r or _is_null(r["next_due"], empty=True):
            continue
        v = r["next_due"]
        if not (isinstance(v, str) and _valid_date(v)):
            errs.append(f"THESIS.kill_criteria_status[{i}].next_due：應為 YYYY-MM-DD 的有效日期或 null，實際為 {v!r}")
        elif isinstance(asof, str) and _valid_date(asof) and v <= asof:
            errs.append(f"THESIS.kill_criteria_status[{i}].next_due：須晚於決策日 {asof}（過去或當天的檢查日永遠不會觸發），"
                        f"實際為 {v!r}；結案或已觸發填 null")
    return errs


def _cross_thesis(o: dict) -> list[str]:
    rows = o.get("r_list_status") if isinstance(o.get("r_list_status"), list) else []
    items = [str(r.get("item")) for r in rows if isinstance(r, dict)]
    if sorted(items) != sorted(f"R{i}" for i in range(1, 11)):
        return [f"THESIS.r_list_status：須 R1–R10 各恰好一列（共 10 列），實際為 {items}"]
    return []


_GRADES = ("A", "B", "C", "D")


_STOP_CODES_B = ("TOO_HARD", "REJECT", "MISSING_DATA", "RED_LIGHT")


def _cross_full_b(o: dict) -> list[str]:
    """FULL_B 摘要：BUY 必須 PASS_ALL 且未停；意願權重（D13）有值時理由須引用 G8 優勢差距與確定性，BUY 時必填。"""
    errs: list[str] = []
    sig, code = o.get("signal"), o.get("reason_code")
    buy = sig == "BUY"
    if buy and (code != "PASS_ALL" or o.get("stopped_at_gate") is not None):
        errs.append("FULL_B：signal=BUY 時 reason_code 必須是 PASS_ALL、stopped_at_gate 必須是 null")
    if sig == "NO_ACTION" and code == "PASS_ALL":   # A3-M3C-07：登錄表會出現無來源的 NO_ACTION 狀態
        errs.append("FULL_B：signal=NO_ACTION 時 reason_code 必須是停止碼（TOO_HARD | REJECT | MISSING_DATA | RED_LIGHT），"
                    "不得為 PASS_ALL；沒有停止就輸出 WATCH")
    if sig == "WATCH" and code in _STOP_CODES_B:   # A3-M3A-02／A3-M3C-07：停止碼只配 NO_ACTION
        errs.append(f"FULL_B：signal=WATCH 時 reason_code 必須是 PASS_ALL（停止碼只配 NO_ACTION；WATCH 停在哪一步寫 "
                    f"stopped_at_gate），實際為 {code!r}；若要停止就輸出 NO_ACTION")
    iw = o.get("intended_weight") if isinstance(o.get("intended_weight"), dict) else {}
    level, reason = iw.get("level"), iw.get("reason")
    if buy and level is None:
        errs.append("FULL_B.intended_weight.level：signal=BUY 時必填（HIGH | MEDIUM | LOW）")
    if level is not None:
        r = unicodedata.normalize("NFKC", reason if isinstance(reason, str) else "").upper()   # 全形、小寫寫法也算
        if "G8" not in r or not ("確定性" in r or "等級" in r or re.search(r"[ABCD]\s*級", r)):
            errs.append("FULL_B.intended_weight.reason：level 有值時必須用一句話引用 G8 優勢差距與確定性等級"
                        f"（須含「G8」與「確定性」「等級」或「B 級」類寫法），實際為 {reason!r}")
    return errs


def _driver(row: Any) -> str | None:
    """driver_id（NFKC、去前後空白；全形與半形寫法視為同一個）；缺或空白 → None。"""
    d = row.get("driver_id") if isinstance(row, dict) else None
    d = unicodedata.normalize("NFKC", d).strip() if isinstance(d, str) else ""
    return d or None


def _max_grade(row: Any) -> str | None:
    """上限型降級（「最高 B／C」類規則）的上限等級；不是上限型 → None（A3-M2B-08）。"""
    g = row.get("max_grade") if isinstance(row, dict) else None
    g = unicodedata.normalize("NFKC", g).strip().upper() if isinstance(g, str) else None
    return g if g in _GRADES else None


def _dup_drivers(rows: Any) -> list[str]:
    seen: list[str] = []
    dup: list[str] = []
    for d in (_driver(r) for r in rows) if isinstance(rows, list) else []:
        if d is None:
            continue
        if d in seen and d not in dup:
            dup.append(d)
        seen.append(d)
    return dup


def downgrade_count(rows: Any) -> int:
    """逐級降級的級數：依獨立驅動因素計，同一 driver_id 只算一次（D18、P-64）；沒有 driver_id 的舊列各算一次。
    上限型列（有 max_grade）不逐級降，由 phase_a_grade 另取上限（A3-M2B-08）。"""
    if not isinstance(rows, list):
        return 0
    ids = [_driver(r) for r in rows if _max_grade(r) is None]
    return len({d for d in ids if d}) + sum(1 for d in ids if d is None)


def _lower(g: str, n: int) -> str:
    return _GRADES[min(_GRADES.index(g) + n, len(_GRADES) - 1)]


def phase_a_grade(pa: dict | None) -> str | None:
    """FULL_A 結束時的確定性等級：starting_grade 依 downgrades 逐項降一級（procedure/06 §1，累計，最低 D；
    同一 driver_id 只降一次，D18），再與上限型列的 max_grade（「最高 C」類，例如盈餘不穩定）取較嚴（A3-M2B-08）。
    無起點（停止或缺）→ None。"""
    g = (pa or {}).get("starting_grade")
    if g not in _GRADES:
        return None
    rows = pa.get("downgrades") if isinstance(pa.get("downgrades"), list) else []
    out = _lower(g, downgrade_count(rows))
    for r in rows:
        mg = _max_grade(r)
        if mg is not None and _GRADES.index(mg) > _GRADES.index(out):
            out = mg
    return out


def _norm_text(t: Any) -> str:
    return re.sub(r"\s+", "", str(t or ""))


def missing_kill_criteria(pa: dict | None, texts: list[Any]) -> list[str]:
    """FULL_A thesis_draft.kill_criteria 中未出現在 texts 的條件原文（忽略空白）；FULL_B 只能新增、不能刪（D29）。"""
    td = (pa or {}).get("thesis_draft")
    src = td.get("kill_criteria") if isinstance(td, dict) else None
    have = {_norm_text(x) for x in texts}
    out = []
    for k in src if isinstance(src, list) else []:
        t = (k.get("text") or k.get("criterion")) if isinstance(k, dict) else k
        if _norm_text(t) and _norm_text(t) not in have:
            out.append(str(t).strip())
    return out


def _has_driver(pa: dict | None, word: str) -> bool:
    rows = (pa or {}).get("downgrades")
    return any(word in (_driver(r) or "") for r in (rows if isinstance(rows, list) else []))


def after_price_grade_cap(pa: dict | None, summary: dict | None, card: dict | None = None) -> tuple[str | None, list[str]]:
    """FULL_B 確定性等級的上限與理由：FULL_A 結束等級（D29，只能降不能升），再加看價後必降項（A3-M2C-01；每個獨立
    驅動因素降一級，前半已因同一 driver 降過者不重複，D18）：
    - 摘要 capital_allocation_net_after_price <0（P-26a 補評，procedure/02 §2）而前半 downgrades 沒有「資本配置」driver；
    - 訊號卡 p08_final.quality＝降級（P-08 最終 <40%，procedure/01 §6）而前半沒有「品質加分」driver（card 給定時才查；
      validate.check 看不到訊號卡，由 FullBHandler.post 另呼叫）。
    沒有前半等級 → (None, [])。"""
    ga = phase_a_grade(pa)
    if ga is None:
        return None, []
    why = [f"FULL_A 結束等級 {ga}（起點 {pa.get('starting_grade')}，D29：後半只能降級）"]
    n = 0
    ca = (summary or {}).get("capital_allocation_net_after_price")
    if _is_num(ca) and ca < 0 and not _has_driver(pa, "資本配置"):
        n += 1
        why.append(f"P-26a 看價後資本配置淨分 {ca} <0、前半未因資本配置降級 → 再降一級（procedure/02 §2）")
    p08 = card.get("p08_final") if isinstance(card, dict) else None
    if isinstance(p08, dict) and p08.get("quality") == "降級" and not _has_driver(pa, "品質加分"):
        n += 1
        why.append("P-08 最終品質＝降級（<40%）、前半未因品質加分降級 → 再降一級（procedure/01 §6）")
    return _lower(ga, n), why


def _cap_full_b_grade(o: dict, pa: dict) -> None:
    """FULL_B certainty_grade 高於 after_price_grade_cap 的上限 → 壓到上限、原值留在 certainty_grade_model、
    理由寫 grade_notes（不報錯：A3-M3D-04，報錯兩次整鏈 MISSING_DATA，與 FULL_B.md「框架以較嚴的結論寫回」一致）。"""
    cap, why = after_price_grade_cap(pa, o)
    gb = o.get("certainty_grade")
    if cap is None or gb not in _GRADES or _GRADES.index(gb) >= _GRADES.index(cap):
        return
    o["certainty_grade_model"] = gb
    o["certainty_grade"] = cap
    o["grade_notes"] = [f"FULL_B 等級 {gb} 高於上限 {cap}，框架壓回 {cap}：" + "；".join(why)]


_CROSS = {
    "FULL_A": _cross_full_a,
    "FULL_B": _cross_full_b,
    "THESIS": _cross_thesis,
}
# 需要前一階段結果的正規化（check 的 ref 參數；只壓低、不報錯，A3-M3D-04）
_NORMALIZE_REF = {
    "FULL_B": _cap_full_b_grade,
}
