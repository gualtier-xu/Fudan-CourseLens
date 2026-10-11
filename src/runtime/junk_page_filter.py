"""Local junk-page screening for the courseware PDF pipeline.

Numpy-free twin of ``worker/courselens_worker/junk_filter.py`` (the M15
dual-hash blacklist plus the U4 featureless/chrome/family stages).  The
managed client runtime ships no numpy, so every signal here is computed
with Pillow alone:

- hashes run in the exact same PIL domain (same ROI crop, resize, blur,
  and default-resample grids), so ``dual_hash`` is bit-identical to the
  worker module and the shipped blacklist stays interchangeable;
- band statistics replace the numpy math with histogram percentiles and
  box-filter row means, which agree with the numpy values well inside the
  frozen thresholds.

The OCR semantic stage stays cloud-only: the local pipeline recognizes no
text, so its deck pass kills only near-blacklist variant families (at
least ``JUNK_FAMILY_MIN_MEMBERS`` members) and never a lone page.  When
editing thresholds or blacklist rows, change the worker module first and
mirror here — ``tests/test_courseware_pdf_junk_local.py`` cross-checks
the shared constants when the worker tree is present.
"""

from __future__ import annotations

from PIL import Image, ImageChops, ImageFilter

JUNK_PAGE_SKIP_REASON = "junk_page"
JUNK_HAMMING_THRESHOLD = 12
JUNK_NORM_SIZE = 64
# Centre content region as (left, top, right, bottom) fractions; must match
# the worker module so the hashes land in one perceptual domain.
JUNK_ROI = (0.20, 0.15, 0.80, 0.85)

JUNK_PAGE_BLACKLIST: tuple[dict[str, str], ...] = (
    {"dhash": "662f5ce3e3f19292", "ahash": "1f81e4383178f8fc", "label": "course-notice-page"},
    {"dhash": "6e0754e363f182d1", "ahash": "2381fc3839f8fafc", "label": "ce-evaluation-notice"},
    {"dhash": "3d23235163530504", "ahash": "808191f9f0f9ffff", "label": "service-hall-portal"},
    {"dhash": "b4661de163719251", "ahash": "1f1f807839fcfff8", "label": "notice-variant-b"},
)

# Frozen V2 thresholds (worker junk_filter); see that module for the
# six-sample calibration record.
JUNK_FEATURELESS_GV_MAX = 8.0
JUNK_AHASH_MIN_BITS = 6
JUNK_BOARD_RANGE_MIN = 60.0
JUNK_CHROME_TOP_FRAC = 0.12
JUNK_CHROME_TOP_EDGE_FRAC = 0.15
JUNK_CHROME_BOTTOM_FRAC = 0.08
JUNK_CHROME_ROWVAR_MAX = 300.0
JUNK_CHROME_EDGE_MIN = 10
JUNK_CHROME_EDGE_DELTA = 8.0
JUNK_CHROME_BOTTOM_VAR_MIN = 1500.0
JUNK_FAMILY_RADIUS = 8
JUNK_FAMILY_MIN_MEMBERS = 3
JUNK_BLACKLIST_NEAR_MAX = 18
# 本地加严（SRC-SYNDROME-1 追加C，实测 826→63 校准：p14/p41 灰度 std≈9.6 的
# 深色近纯板与 p62 std≈4.8 的近白页漏网）。方差 < 144（std < 12）且 range
# 守卫（< JUNK_BOARD_RANGE_MIN）通过的近纯页判垃圾——真实内容页即便只有
# 1% 非背景像素 std 也 ≥15，不受影响。仅本地管线生效（本地无 OCR 语义兜底，
# 宁严勿漏）；worker 冻结阈值不动、常量不与 worker 共名，跨检测试不受扰。
JUNK_LOCAL_FEATURELESS_GV_MAX = 144.0
# 相邻帧去重上限（dHash 汉明距离 ≤5）：未翻页的连续快照成对出现（实测
# p20/p36 与前页 dH=0，另有约 8 对 dH≤5）。仅本地管线生效。
JUNK_LOCAL_ADJACENT_DHASH_MAX = 5
# U8②（SRC-CLEANUP-1）设备/系统占位画面族判据——仅本地管线生效。冻结阈值
# 来自 2026-09-22 五讲 437 页离线重放校准：Miracast 投屏待机页、显示器关机
# 确认框、教室桌面壁纸都是「大面积均匀背景+孤立小块内容」——非纯色（灰度
# std 26-45，远过 featureless 的 std<12 线），但结构远弱于真实内容页。单帧
# 统计无法通分：真稀疏标题页（「Thanks!」页 std≈28.7）与待机页（std≈26.3）
# 灰度几乎重合、全幅边缘密度反被稀疏内容页压低——故判定必须带全讲上下文
# （deck pass），以本讲内容中位 std 为锚，双条件同时满足才判占位（宁漏勿杀）：
#   ①绝对结构窗：std < STD_ABS_MAX 且 range < RANGE_MAX（近纯板守卫类似形）
#   ②相对锚：std < 本讲保留页中位 std × STD_MEDIAN_RATIO
# 校准记录：当日讲次（中位 95.5）六张占位页 std 26.3-45.3 全灭（52→46），
# 内容页 min std 62.2 全存；跨讲次仅 807893-p23（教室桌面真占位）同灭，
# 38b-p28/p61「Thanks」、413-p32 等真稀疏标题页全部存活。
JUNK_PLACEHOLDER_STD_ABS_MAX = 55.0
JUNK_PLACEHOLDER_RANGE_MAX = 200.0
JUNK_PLACEHOLDER_STD_MEDIAN_RATIO = 0.5
# 锚稳定性守卫：保留页不足此数不判（中位数无意义），宁漏勿杀。
JUNK_PLACEHOLDER_MIN_DECK = 8
# 闭集跳过原因（与 courseware_pdf.PAGE_SKIPS 同笔登记）。
DEVICE_PLACEHOLDER_SKIP_REASON = "device_placeholder"


def _normalized(image: Image.Image) -> Image.Image:
    """ROI-crop, normalize, and denoise one frame into the hashing domain."""
    left, top, right, bottom = JUNK_ROI
    width, height = image.size
    box = (
        int(width * left),
        int(height * top),
        int(width * right),
        int(height * bottom),
    )
    crop = image
    if box[2] - box[0] >= 2 and box[3] - box[1] >= 2:
        crop = image.crop(box)
    gray = crop.convert("L").resize((JUNK_NORM_SIZE, JUNK_NORM_SIZE))
    return gray.filter(ImageFilter.GaussianBlur(1))


def dual_hash(image: Image.Image) -> tuple[str, str]:
    """Return ``(dhash_hex, ahash_hex)`` — bit-identical to the worker port."""
    normalized = _normalized(image)
    dgrid = normalized.resize((9, 8)).tobytes()
    dvalue = 0
    for row in range(8):
        base = row * 9
        for column in range(8):
            dvalue = (dvalue << 1) | int(dgrid[base + column + 1] > dgrid[base + column])
    agrid = normalized.resize((8, 8)).tobytes()
    amean = sum(agrid) / 64.0
    avalue = 0
    for value in agrid:
        avalue = (avalue << 1) | int(value > amean)
    return f"{dvalue:016x}", f"{avalue:016x}"


def hamming_distance(left: str, right: str) -> int:
    """Count differing bits between two same-width hex hashes."""
    return (int(left, 16) ^ int(right, 16)).bit_count()


def entry_for(image: Image.Image, label: str) -> dict[str, str]:
    """Build one blacklist row from a junk-page screenshot."""
    dhash_hex, ahash_hex = dual_hash(image)
    return {"dhash": dhash_hex, "ahash": ahash_hex, "label": label}


def junk_match(
    image: Image.Image,
    blacklist: tuple[dict[str, str], ...] | None = None,
) -> tuple[bool, str]:
    """Return ``(is_junk, matched_label)`` under the dual-hash OR rule."""
    rows = JUNK_PAGE_BLACKLIST if blacklist is None else blacklist
    dhash_hex, ahash_hex = dual_hash(image)
    for row in rows:
        if (
            hamming_distance(dhash_hex, row["dhash"]) <= JUNK_HAMMING_THRESHOLD
            or hamming_distance(ahash_hex, row["ahash"]) <= JUNK_HAMMING_THRESHOLD
        ):
            return True, str(row.get("label") or "")
    return False, ""


def _population_variance(values: list[int]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


def _variance_from_histogram(hist: list[int], total: int) -> float:
    if total <= 0:
        return 0.0
    weighted = sum(value * count for value, count in enumerate(hist))
    mean = weighted / total
    acc = 0.0
    for value, count in enumerate(hist):
        acc += count * (value - mean) ** 2
    return acc / total


def _percentile_from_histogram(hist: list[int], total: int, q: float) -> float:
    """Linear-interpolated percentile over integer sample values (numpy parity)."""
    if total <= 0:
        return 0.0
    rank = (total - 1) * (q / 100.0)
    lower = int(rank)
    frac = rank - lower

    def value_at(position: int) -> int:
        cumulative = 0
        for value, count in enumerate(hist):
            cumulative += count
            if cumulative > position:
                return value
        return 255

    low = value_at(lower)
    high = value_at(lower + 1) if lower + 1 < total else low
    return low + (high - low) * frac


def _row_statistics(gray: Image.Image, top_frac: float) -> tuple[list[int], int]:
    """Row means of the top ``top_frac`` band, via a box-filter column squeeze."""
    width, height = gray.size
    band_h = max(1, int(height * top_frac))
    band = gray.crop((0, 0, width, band_h))
    column = band.resize((1, band_h), Image.BOX)
    return list(column.tobytes()), band_h


def _subsample_variance(gray: Image.Image) -> float:
    """Population variance over gray[::4, ::4] — the exact numpy pixel set.

    Row-aware byte slicing (one byte per pixel in L mode) picks the same
    pixels as numpy's strided slice, so the value matches the worker's
    float32 computation up to float precision.
    """
    width, height = gray.size
    data = gray.tobytes()
    hist = [0] * 256
    picked = 0
    for y in range(0, height, 4):
        row = data[y * width:(y + 1) * width]
        for value in row[::4]:
            hist[value] += 1
            picked += 1
    return _variance_from_histogram(hist, picked)


def frame_features(image: Image.Image) -> dict[str, object]:
    """Compute every screening signal for one frame in a single pass.

    Mirrors the worker ``frame_features`` keys so a features dict is
    interpretable on both sides.
    """
    gray = image.convert("L")
    width, height = gray.size

    hist = gray.histogram()
    total = width * height

    top_rows, _top_h = _row_statistics(gray, JUNK_CHROME_TOP_FRAC)
    edge_rows, edge_h = _row_statistics(gray, JUNK_CHROME_TOP_EDGE_FRAC)
    if edge_h >= 2:
        width_e, _ = gray.size
        edge_band = gray.crop((0, 0, width_e, edge_h))
        diff = ImageChops.difference(
            edge_band.crop((0, 0, width_e, edge_h - 1)),
            edge_band.crop((0, 1, width_e, edge_h)),
        )
        edge_rows = list(diff.resize((1, edge_h - 1), Image.BOX).tobytes())
    bottom_start = height - max(1, int(height * JUNK_CHROME_BOTTOM_FRAC))
    bottom_band = gray.crop((0, bottom_start, width, height))
    bottom_h = height - bottom_start
    bottom_rows = list(bottom_band.resize((1, bottom_h), Image.BOX).tobytes())

    dhash_hex, ahash_hex = dual_hash(image)
    top_var = _population_variance(top_rows)
    strong_edges = sum(1 for value in edge_rows if value > JUNK_CHROME_EDGE_DELTA)
    bottom_var = _population_variance(bottom_rows)
    near = min(
        min(hamming_distance(dhash_hex, row["dhash"]), hamming_distance(ahash_hex, row["ahash"]))
        for row in JUNK_PAGE_BLACKLIST
    )
    return {
        "gv": _subsample_variance(gray),
        "range": _percentile_from_histogram(hist, total, 99)
        - _percentile_from_histogram(hist, total, 1),
        "dhash": dhash_hex,
        "ahash": ahash_hex,
        "ahash_bits": bin(int(ahash_hex, 16)).count("1"),
        "top_var": top_var,
        "strong_edges": strong_edges,
        "bottom_var": bottom_var,
        "chrome": (
            top_var < JUNK_CHROME_ROWVAR_MAX
            and strong_edges >= JUNK_CHROME_EDGE_MIN
            and bottom_var > JUNK_CHROME_BOTTOM_VAR_MIN
        ),
        "weak_chrome": (
            (top_var < JUNK_CHROME_ROWVAR_MAX and strong_edges >= JUNK_CHROME_EDGE_MIN)
            or bottom_var > JUNK_CHROME_BOTTOM_VAR_MIN
        ),
        "near": int(near),
    }


def featureless_verdict(features: dict[str, object]) -> bool:
    """Stage ① — blank/near-solid pages, with the dark-board exemption."""
    if float(features["range"]) >= JUNK_BOARD_RANGE_MIN:
        return False
    gv = float(features["gv"])
    if gv < JUNK_FEATURELESS_GV_MAX:
        return True
    if int(features["dhash"], 16) == 0:
        return True
    if int(features["ahash_bits"]) < JUNK_AHASH_MIN_BITS:
        return True
    # 本地加严段（追加C）：std 5-12 的近纯板面（深色渐变板/近白页）此前漏网；
    # range 守卫已把强伪影页挡在前面。
    return gv < JUNK_LOCAL_FEATURELESS_GV_MAX


def chrome_verdict(features: dict[str, object]) -> bool:
    """Stage ② — full-window browser screenshot by band structure alone."""
    return bool(features["chrome"])


def information_density(image: Image.Image) -> float:
    """全幅灰度 std（信息密度代理，U8① 相邻帧组内保最大张用）。

    与 ``frame_features`` 的 ``gv`` 同源同域（同一子采样方差开方）；
    板书渐增的组内后帧密度更高，擦板反向由「仅严格更密才替换」防住。
    """
    return float(_subsample_variance(image.convert("L"))) ** 0.5


def placeholder_verdict(
    features: dict[str, object], deck_median_std: float | None
) -> bool:
    """设备/系统占位画面判定（U8②，deck pass 用）。

    ``deck_median_std`` 传 ``None`` 时退化为仅绝对窗（单页测试场景）；
    管线内必须传全讲保留页中位 std——没有讲次锚就没有判定权。
    """
    std = float(features["gv"]) ** 0.5
    if std >= JUNK_PLACEHOLDER_STD_ABS_MAX:
        return False
    if float(features["range"]) >= JUNK_PLACEHOLDER_RANGE_MAX:
        return False
    if deck_median_std is None:
        return True
    return std < float(deck_median_std) * JUNK_PLACEHOLDER_STD_MEDIAN_RATIO


def page_is_junk(image: Image.Image, features: dict[str, object] | None = None) -> bool:
    """Pipeline verdict: blacklist OR featureless OR chrome (no OCR needed)."""
    if features is None:
        features = frame_features(image)
    for row in JUNK_PAGE_BLACKLIST:
        if (
            hamming_distance(str(features["dhash"]), row["dhash"]) <= JUNK_HAMMING_THRESHOLD
            or hamming_distance(str(features["ahash"]), row["ahash"]) <= JUNK_HAMMING_THRESHOLD
        ):
            return True
    return featureless_verdict(features) or chrome_verdict(features)


def near_duplicate_families(
    records: list[dict[str, object]],
    radius: int = JUNK_FAMILY_RADIUS,
    min_members: int = JUNK_FAMILY_MIN_MEMBERS,
) -> list[list[int]]:
    """Stage ③ — seed-cluster the near-miss survivors; return index groups."""
    eligible = [
        index for index, record in enumerate(records)
        if int(record.get("near") or 0) <= JUNK_BLACKLIST_NEAR_MAX
    ]
    families: list[list[int]] = []
    pool = list(eligible)
    while pool:
        seed = pool.pop(0)
        family, rest = [seed], []
        for index in pool:
            record = records[index]
            seed_record = records[seed]
            if (
                hamming_distance(str(seed_record["dhash"]), str(record["dhash"])) <= radius
                or hamming_distance(str(seed_record["ahash"]), str(record["ahash"])) <= radius
            ):
                family.append(index)
            else:
                rest.append(index)
        pool = rest
        if len(family) >= min_members:
            families.append(family)
    return families


def adjudicate_deck_local(records: list[dict[str, object]]) -> list[bool]:
    """Text-free deck pass: kill near-blacklist variant families only.

    The worker's semantic stage (④) needs recognized wording, which the
    local pipeline never has; a lone weak-chrome page therefore survives
    here and stays a cloud-side decision.
    """
    verdicts = [False] * len(records)
    for family in near_duplicate_families(records):
        for index in family:
            verdicts[index] = True
    return verdicts
