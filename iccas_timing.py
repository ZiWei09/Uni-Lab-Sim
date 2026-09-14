"""Recipe-based post-processing schedules, separate from handshake test timing."""
from __future__ import annotations

import math
from pathlib import Path

import yaml


ESTIMATES = {"transfer_s", "liquid_flow_ml_s", "atomization_cycle_s", "spray_filtration_s",
             "needle_clean_s", "vacuum_pumpdown_s", "valve_settle_s"}
FILTER_CLEANERS = {"吸液针外壁清洗": None, "吸液针清洗罐清洗": "吸液针清洗罐",
                   "原液管清洗回收罐清洗": "原液管清洗回收罐", "过滤罐清洗": "过滤罐清洗接水盘"}


class ProcessTiming:
    def __init__(self, path=None):
        path = Path(path) if path else Path(__file__).parent / "config/iccas_timing.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or set(data.get("estimates", {})) != ESTIMATES:
            raise ValueError("Invalid ICCAS timing profile")
        self.estimates = data["estimates"]
        for key, value in self.estimates.items():
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Timing estimate must be finite and positive: {key}")

    def plan(self, kind, trigger, read):
        """Return a frozen schedule; client edits after Trigger affect only the next run."""
        stages = []

        def n(name):
            value = read(name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"时间/液量/次数必须为有限非负数: {name}")
            return value

        def seconds(name):
            return n(name) / 1000.0

        def add(name, duration, source):
            if duration > 0:
                stages.append({"name": name, "process_seconds": duration, "source": source})

        def fill(name, volume, cycles=1):
            add(name, volume / self.estimates["liquid_flow_ml_s"] * cycles, "estimated_flow")

        base = trigger.removesuffix("触发PCtoPLC")
        if "取料" in base or "放料" in base:
            add("搬运与定位", self.estimates["transfer_s"], "estimated_motion")
        elif kind == "spray" and base.endswith("后处理雾化"):
            lane = base[:3]  # A工位 / B工位
            fill("初始加水", n(lane + "最开始加水量"))
            add("喷雾进料", n(lane + "抽原液次数") * self.estimates["atomization_cycle_s"], "estimated_cycle")
            add("抽滤前搅拌/熟化", seconds(lane + "抽滤前搅拌时间"), "recipe_ms")
            add("首次过滤", self.estimates["spray_filtration_s"], "estimated_filtration")
            for index in (1, 2):
                cycles = n(f"{lane}第{index}次粉末洗涤次数")
                fill(f"洗涤组 {index} 加水（{cycles} 轮合计）", n(f"{lane}第{index}次洗涤加水量"), cycles)
                add(f"洗涤组 {index} 搅拌（{cycles} 轮合计）",
                    seconds(f"{lane}第{index}次粉末搅拌时间") * cycles, "recipe_ms")
                add(f"洗涤组 {index} 过滤（{cycles} 轮合计）",
                    self.estimates["spray_filtration_s"] * cycles, "estimated_filtration")
        elif kind == "spray" and base.endswith("搅拌桨清洗"):
            lane = base[:3]
            for solvent in ("清水", "NMP", "丙酮"):
                prefix = lane + solvent + "搅拌桨清洗"
                cycles = n(prefix + "次数")
                fill(solvent + "清洗加液", n(prefix + "加注"), cycles)
                add(solvent + "清洗等待", seconds(prefix + "等待时间") * cycles, "recipe_ms")
                add(solvent + "抽废", seconds(prefix + "抽废时间") * cycles, "recipe_ms")
            add("管路吹扫", seconds(lane + "管道吹气时间"), "recipe_ms")
        elif kind == "spray" and base == "移液针清洗":
            add("移液针清洗（点表无配方参数）", self.estimates["needle_clean_s"], "estimated_cleaning")
        elif kind == "filter" and base == "抽原液启动":
            add("抽原液/过滤", seconds("原液抽取时间PCtoPLC"), "recipe_ms")
            add("过滤吹气", seconds("过滤罐过滤吹气时间PCtoPLC"), "recipe_ms")
        elif kind == "filter" and base == "真空箱除泡启动":
            add("抽真空", self.estimates["vacuum_pumpdown_s"], "estimated_pumpdown")
            add("真空除泡保持", seconds("真空箱_真空除泡时间PCtoPLC"), "recipe_ms")
            add("泄压", seconds("真空箱泄压时间PCtoPLC"), "recipe_ms")
        elif kind == "filter" and base in FILTER_CLEANERS:
            for solvent in ("清水", "NMP", "丙酮"):
                cycles = n(f"{base}_{solvent}次数PCtoPLC")
                # Supplier field is a time LIMIT, not an actual duration.
                # Use it as a conservative schedule estimate, explicitly labeled.
                fill_s = n(f"{base}_吸{solvent}量PCtoPLC") / self.estimates["liquid_flow_ml_s"]
                limit_s = seconds(f"{base}_吸{solvent}时间上限PCtoPLC")
                add(solvent + "吸液", (limit_s if limit_s > 0 else fill_s) * cycles, "time_limit_estimate")
            add("吸液后吹气", seconds(base + "_吸液后吹气时间PCtoPLC"), "recipe_ms")
            waste = FILTER_CLEANERS[base]
            if waste:
                for field in ("抽废时间上限", "真空罐进废液时间", "真空罐排废液时间"):
                    add(field, seconds(f"{waste}_{field}PCtoPLC"),
                        "time_limit_estimate" if "上限" in field else "recipe_ms")
            if base == "原液管清洗回收罐清洗":
                add("原液管道吹气", seconds("原液管道吹气时间PCtoPLC"), "recipe_ms")
                add("原液管道抽液", seconds("原液管道清洗_抽液时间PCtoPLC"), "recipe_ms")
        else:
            raise ValueError(f"未建模工艺时间: {kind}/{trigger}")
        if not stages:
            add("阀门切换/稳定", self.estimates["valve_settle_s"], "estimated_settle")
        return stages


def demo_recipe(kind):
    """Explicit opt-in initial recipe snapshot; zero client values remain zero."""
    values = {}
    if kind == "spray":
        for lane in ("A", "B"):
            for suffix, value in {"最开始加水量": 500.0, "抽原液次数": 1,
                                  "抽滤前搅拌时间": 18_000_000, "第1次洗涤加水量": 500.0,
                                  "第1次粉末搅拌时间": 300_000, "第1次粉末洗涤次数": 1,
                                  "NMP搅拌桨清洗加注": 500.0, "NMP搅拌桨清洗次数": 1,
                                  "NMP搅拌桨清洗等待时间": 30_000,
                                  "NMP搅拌桨清洗抽废时间": 30_000, "管道吹气时间": 15_000}.items():
                values[lane + "工位" + suffix] = value
    elif kind == "filter":
        values.update({"原液抽取时间PCtoPLC": 1_200_000, "过滤罐过滤吹气时间PCtoPLC": 30_000,
                       "真空箱_真空除泡时间PCtoPLC": 7_920_000, "真空箱泄压时间PCtoPLC": 0,
                       "原液管道吹气时间PCtoPLC": 15_000, "原液管道清洗_抽液时间PCtoPLC": 30_000})
        for base, waste in FILTER_CLEANERS.items():
            values.update({base + "_吸清水量PCtoPLC": 300.0, base + "_清水次数PCtoPLC": 1,
                           base + "_吸清水时间上限PCtoPLC": 30_000, base + "_吸液后吹气时间PCtoPLC": 15_000})
            if waste:
                for suffix, value in {"抽废时间上限": 60_000, "真空罐进废液时间": 30_000,
                                      "真空罐排废液时间": 30_000}.items():
                    values[f"{waste}_{suffix}PCtoPLC"] = value
    return values
