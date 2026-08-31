"""知识库评估查询生成器 — 从真实登记册自动生成:
  short 短问句（全量，像用户随手敲的）
  news  事故简报（全量，自然叙述，不点名具体文件）
  long  长文事故报道（抽查子集，≥500字，信息密集）

输入：backend/data/saferag.db 的 kb_files 登记表（filename/region/city/file_type）。
输出：JSON 数组，每条：
    {
      "q": 检索提问（news/long 不包含任何具体法规书名）
      "gold": 期望命中的来源文件（[filename]）
      "kind": "short" | "news" | "long"
      "file_types": ["地方法规"], "provinces": [region], "cities": [city]
    }

喂给 scripts/eval_retrieval.py --queries … 做「带地域/类型筛选 vs 基线」的
hit@k / MRR 对比。gold=本文件，测的是检索回捞能力 + 过滤不过杀。
"""

import argparse
import json
import os
import random
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "backend", "data", "saferag.db")

_REG = re.compile(r"(省|市|自治区|壮族自治区|回族自治区|维吾尔自治区|特别行政区)$")
_SUFFIX = ("管理办法", "管理条例", "管理规定", "管理暂行办法", "实施细则",
           "暂行规定", "若干规定", "规定", "条例", "办法", "细则", "规程", "规章")
_DOCX = re.compile(r"\.docx?$", re.IGNORECASE)

# 事件域词典：文件名命中关键词 → (事发场景, 事故情节)。未命中走通用情节。
_DOMAINS = [
    ("消防", "商场", "突发火情，火势迅速蔓延，现场浓烟弥漫，有人员被困"),
    ("危险化学", "化工企业生产储运区", "储罐阀门失效引发物料泄漏并起火爆燃，周边群众紧急疏散"),
    ("危险废物", "生产型企业厂区", "违规将危险废物倾倒于市政管网，造成周边水体与土壤污染"),
    ("建筑", "建筑施工工地", "塔吊吊装过程中钢丝绳断裂，构件坠落砸中下方作业区"),
    ("矿山", "煤矿井下采掘工作面", "发生顶板冒落事故，多名矿工被困井下"),
    ("特种设备", "在用工业锅炉房", "锅炉运行中压力异常升高，安全阀动作后仍发生介质泄漏"),
    ("电梯", "居民住宅楼", "电梯运行中突发困人，轿厢内乘客滞留较长时间"),
    ("烟花爆竹", "城乡结合部集中燃放区", "燃放余火引燃周边堆放的易燃物，引发大面积火情"),
    ("食品", "校外供餐单位食堂", "多人用餐后出现呕吐腹泻等疑似食物中毒症状，集体送医"),
    ("燃气", "老城区居民住宅区", "发生燃气泄漏并引发爆炸，多间房屋受损、门窗玻璃震碎"),
    ("道路", "国省道某弯道路段", "一辆客车与同向货车追尾碰撞，车辆失控侧翻"),
    ("电力", "变电站外高压输电通道", "施工机械误碰导线引发线路跳闸，导致区域大面积停电"),
    ("水利", "流域控制性水库", "持续强降雨致库水位快速上涨，逼近汛限水位，下游面临洪涝风险"),
    ("渔业", "渔港避风港池", "渔船遇强风袭击发生倾覆，船员落水失联"),
    ("出租屋", "城中村自建出租屋", "屋内电动车违规充电起火，浓烟沿楼道蔓延，封堵逃生通道"),
    ("学校", "学校在建教学楼改造现场", "高处作业人员未系安全带，从脚手架坠落受伤"),
    ("运输", "客运站待发客车", "运营途中突发故障，车辆失控撞向护栏"),
    ("疫苗", "基层预防接种门诊", "集中接种后出现多名受种者发热、红肿等疑似异常反应"),
    ("森林", "重点林区作业现场", "林区作业期间发生森林火情，火势沿山体蔓延，扑救一度艰难"),
    ("林业", "林业经营单位作业现场", "采伐作业中发生生产安全事故，有作业人员受伤"),
    ("农业", "农业生产经营场所", "田间农机作业时发生翻覆事故，操作人员被困"),
    ("农机", "农机作业现场", "农机具在作业过程中发生机械伤害事故"),
    ("劳动保护", "用人单位作业现场", "作业人员发生职业健康损害事件，现场防护措施缺失"),
    ("职业健康", "用人单位作业现场", "作业场所职业危害因素超标，多名劳动者出现身体不适"),
    ("通信", "通信设施作业现场", "通信线路施工作业中发生触电事故"),
]

# 长文补料库：随机塞几句，把叙述撑到 ≥500 字且信息自然
_LONG_FILLERS = [
    "事发前一日，当地气象部门发布了大风降温预警，现场作业条件较为不利。",
    "事发地点地处城区人口密集区域，影响范围较广，附近学校和医院已同步加强警戒。",
    "事故发生后，当地应急指挥中心迅速启动应急响应，调派多支救援队伍赶赴现场。",
    "现场勘查显示，事发区域安全警示标识缺失，部分防控措施停留在纸面。",
    "周边居民反映，该区域此前曾出现过类似苗头，但未引起足够重视。",
    "事故调查组已对相关设备、台账及现场人员进行取证，同步开展技术鉴定。",
    "当地政府部门已就此召开专题会议，部署在全市范围内开展同类风险排查。",
    "医疗救治组反馈，多数伤者经处理后情况稳定，个别重伤者仍在观察治疗。",
    "纪委、监委已介入，对相关部门履职情况开展同步核查。",
    "事发企业的安全管理人员称，历年检查中曾多次提出整改要求，但落实不到位。",
]


def _place_of(region: str, city: str) -> str:
    if city:
        return _REG.sub("", city.strip())
    if region:
        return _REG.sub("", region.strip())
    return "本地"


def _domain_of(filename: str) -> tuple[str, str] | None:
    """文件名命中领域词 → (事发场景, 事故情节)；没命中返回 None。

    返回 None 的文件**不生成 news/long**：故障叙述造不出相关情节，硬配 gold 就是
    "福建普通事故 → 期望《森林条例》"这种驴唇不对马嘴的假样本。
    """
    for kw, scene, incident in _DOMAINS:
        if kw in filename:
            return scene, incident
    return None


def _time_str(rng: random.Random) -> str:
    return (f"{rng.randint(2023, 2026)}年{rng.randint(1, 12)}月{rng.randint(1, 28)}日"
            f"{rng.choice(['凌晨', '上午', '午后', '傍晚', '夜间'])}")


def _news_body(place: str, scene: str, incident: str, rng: random.Random) -> str:
    n = rng.randint(3, 9)
    m = rng.randint(1, min(3, n))
    loss = rng.randint(50, 800)
    out = (
        f"{_time_str(rng)}，{place}{scene}{incident}。"
        f"事故造成{n}人受伤，其中{m}人伤势较重被转送医院救治，"
        f"直接经济损失初步估计约{loss}万元。"
        f"属地应急、消防、卫健等部门第一时间到场联动处置，"
        f"现场已拉起警戒并对周边人员实施疏散，事故调查组随后进驻开展原因与责任核查。"
        f"作为安全生产监管分析人员，请结合上述情况，分析本次事故暴露出的安全管理问题"
        f"以及相关责任应如何认定和追究，应适用哪些规定开展处置。"
    )
    return out


def _long_body(place: str, scene: str, incident: str, rng: random.Random) -> str:
    """构造 ≥500 字的长篇事故报道。"""
    n = rng.randint(4, 12)
    m = rng.randint(1, min(4, n))
    loss = rng.randint(100, 2000)
    parts = [
        f"{_time_str(rng)}，{place}{scene}{incident}，现场一度十分危急。",
        f"据现场目击者描述，事发时作业活动正在进行，企业一线人员应急处置能力明显不足。",
        f"救援力量抵达后，第一时间封锁周边区域、组织人员有序疏散，并调集专业装备开展抢险。",
        f"事故共造成{n}人受伤，其中{m}人伤势较重，被紧急送往附近医院救治，另有若干人轻微擦伤；"
        f"直接经济损失初步估计约{loss}万元，具体仍在核查。",
        f"当地应急管理、消防救援、卫生健康等部门随即成立联合处置组，"
        f"同步开展伤员救治、现场勘查、物证提取和原因调查，事故调查组已正式进驻事发单位。",
    ]
    # 随机补料直到 ≥500 字
    fillers = list(_LONG_FILLERS)
    rng.shuffle(fillers)
    for filler in fillers:
        if sum(len(p) for p in parts) >= 500:
            break
        parts.append(filler)
    parts.append(
        "初步核查显示，事发区域此前多次被检查出安全管理漏洞，整改要求未真正闭环，"
        "相关责任落实不到位。附近居民与企业职工对该区域的安全状况早有担忧。"
        "作为安全生产监管分析人员，请结合报道反映的现场情况，梳理这起事故暴露出的"
        "安全管理薄弱环节，分析事发单位及相关部门的责任划分，并说明后续隐患排查、"
        "整改提升应依据哪些地方法规要求来组织推进。"
    )
    return "".join(parts)


def gen(kf: dict, rng: random.Random, long: bool) -> list[dict]:
    filename = kf["filename"]
    region, city = kf.get("region") or "", kf.get("city") or ""
    place = _place_of(region, city)
    domain = _domain_of(filename)

    base = {
        "gold": [filename],
        "file_types": ["地方法规"],
        "provinces": [region] if region else [],
        "cities": [city] if city else [],
        "doc": filename,
    }
    queries = [{**base, "kind": "short", "q": f"{place}的{_short_topic(filename)}有哪些规定要求？"}]
    if domain is not None:  # 只有领域能匹配才生 news/long，保证故障叙述与 gold 相关
        scene, incident = domain
        queries.append({**base, "kind": "news", "q": _news_body(place, scene, incident, rng)})
        if long:
            queries.append({**base, "kind": "long", "q": _long_body(place, scene, incident, rng)})
    return queries


_LEADING_PLACE = re.compile(r"^[一-龥]{2,8}?(省|市|自治区|自治州|地区|盟)")


def _short_topic(filename: str) -> str:
    """从文件名凝出短问句主题（剥书名/开头地名/后缀），避免与 place 重复。"""
    name = _DOCX.sub("", filename).strip("《》")
    name = _LEADING_PLACE.sub("", name, count=1)  # 剥"三亚市/齐齐哈尔市/内蒙古自治区"
    for suf in _SUFFIX:
        if name.endswith(suf):
            name = name[: -len(suf)]
            break
    return name or "安全生产"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default=None,
                    help="输出 JSON（默认 scripts/eval_queries.auto.json）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--long-count", type=int, default=40,
                    help="生成多少条 long（≥500字），0=不生成")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT filename, region, city, file_type FROM kb_files "
        "WHERE status='ready' ORDER BY filename").fetchall()
    conn.close()
    rows = [dict(r) for r in rows]
    print(f"登记册 ready 文件: {len(rows)}")

    rng = random.Random(args.seed)
    # long 抽样：只在"能匹配领域（能造出相关叙述）"的文件里均匀取，避免假样本
    cand = [i for i, r in enumerate(rows) if _domain_of(r["filename"]) is not None]
    long_idx: set[int] = set()
    if args.long_count and len(cand) > 1:
        chosen = {round(i * (len(cand) - 1) / (args.long_count - 1))
                  for i in range(args.long_count)}
        long_idx = {cand[i] for i in sorted(chosen)[: args.long_count]}

    queries = []
    no_domain = 0
    for i, r in enumerate(rows):
        if _domain_of(r["filename"]) is None:
            no_domain += 1
        queries.extend(gen(r, rng, long=(i in long_idx)))

    out = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "eval_results", "eval_queries.auto.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(queries, f, ensure_ascii=False, indent=1)

    kinds, over = {}, 0
    for q in queries:
        kinds[q["kind"]] = kinds.get(q["kind"], 0) + 1
        if len(q["q"]) >= 500:
            over += 1
    print(f"生成 {len(queries)} 条（{kinds}），其中 ≥500字 长文 {over} 条")
    print(f"（{no_domain} 个文件无领域匹配，只生成了 short，没有 news/long）")
    print(f"写出: {out}")
    print("\n=== 样例 news / long ===")
    for q in queries:
        if q["kind"] in ("news", "long"):
            print(f"\n[{q['kind']}] ({len(q['q'])}字) {q['q'][:120]}…")
            if q["kind"] == "long":
                print(f"        gold={q['gold'][0]} | {q['provinces']} {q['cities']}")
                break


if __name__ == "__main__":
    main()