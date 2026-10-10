# Visual regression baseline

视觉回归基线（VISUAL-BASELINE，0.1.1 首批）给全站建立截图 diff 回归检测：
对合成壳服务器服务的真实前端做全页截图，与已验收基线逐张像素比对。它与既有的
computed 样式对照行为钉（`tests/frontend_*_behavior.mjs`、
`tests/test_frontend_workbench.py` 里的 getComputedStyle 级断言）互补——行为钉
钉「单条样式合同」，截图基线钉「整页最终呈现」。

## 覆盖面与预算

- 表面：`study`（默认空态）/ `live` / `settings` / `data` / `onboarding`（账户
  菜单真实入口打开，非裸切页）× 主题 `light` + `dark` × 视口
  `375x812` / `768x1024` / `1440x900` = **30 组合**。
- 基线：`tests/visual-baseline/baselines/*.jpg`，每张 ≤300KB（超限自动降
  JPEG 质量，`--max-bytes` 可配），全量约 1.5MB。
- 运行时长：全组合 ≤3 分钟（正典机器实测约 30s，含合成壳启动）；pytest
  包装器内建 180s 预算断言。

## 组件

| 文件 | 职责 |
| --- | --- |
| `tests/visual-baseline/capture.mjs` | playwright（headless chromium）采集 harness：起合成壳 → 30 组合截图 → manifest |
| `tests/visual-baseline/visual_compare.py` | Pillow 像素比对：容差、失败阈值、热力图、`--update` |
| `tests/test_visual_baseline.py` | 正典 pytest 包装器（playwright 缺席=SKIP，不拖垮全量） |
| `scripts/check_visual_baseline.py` | 人类/CI 一键入口：自检或 `--update` 重采集 |
| `tests/visual-baseline/package.json` | 锁定 playwright 版本（node_modules 已 gitignore） |

依赖安装（一次性，本机）：

```bash
cd tests/visual-baseline && npm install && npx playwright install chromium
```

合成壳需要完整客户端依赖（含 Pillow），harness 默认用正典
`.venv-client-py310`，或经 `COURSELENS_VISUAL_PYTHON` 指定。

## 确定性方案（动态内容归一）

截图基线的最大技术点是动态内容。本 harness 的归一三层：

1. **客户端时钟冻结**：`addInitScript` 在任何产品脚本前把 `Date` 整体替换为
   固定时刻（本地 2026-10-20 10:08 周二）——问候语时段档/星期/节日、诗联
   确定性轮换（`hash(日期, 时段档)`）、课表「今日/本周」选择、主题「自动」
   解析全部归一；浏览器时区钉 `Asia/Shanghai`、locale 钉 `zh-CN`，任何机器
   上冻结的是同一面本地壁钟。服务器时间戳无论真值，经冻结 `Date` 格式化后
   输出恒同。
2. **主题装配前显式写入**：`localStorage["courselens.theme.v2"]` 在页面脚本
   执行前 seed 为 light/dark，绕开「自动」档对本机时刻的依赖。
3. **韵律与瞬态归一**：context 级 `reducedMotion` + 截图 `animations:
   "disabled"` 关过渡动画；toast 区域截图前遮蔽。

启动竞态防线：每次采集先等认证就绪（顶栏账户簇=「账户」），导航后校验
「目标页激活 + 该页无加载中占位」才落快门，不达预期整段重试（≤3 次）——
boot 中途态绝不进基线。校验原则=只校验进像素的可见面（例：学习页默认拍
空态，隐藏的 live 卡内部状态不校验）。

## 基线更新流程（有意视觉变更后）

1. 完成视觉改动并跑过该改动的定向行为测试。
2. 人工核对渲染效果后重采集：

   ```bash
   python scripts/check_visual_baseline.py --update
   ```

   或等价地 `COURSELENS_VISUAL_UPDATE=1 python -m pytest -q
   tests/test_visual_baseline.py`。`--update` 先跑一次比对留痕（超差清单+
   热力图），再把新截图提升为基线。
3. `git diff` 逐张目检基线图片变化是否符合改动预期（unexpected 漂移=查
   确定性方案是否被新表面破坏），然后随改动同一笔提交。
4. 无意漂移（没改视觉却报警）先看热力图定位区域：动效/时机类漂移检查
   新代码是否引入时序敏感渲染；数据类漂移检查合成种子是否漂移。

像素判定规则：任一通道差 > `--pixel-tolerance`（默认 6/255）记变化像素，
变化占比 > `--fail-ratio`（默认 0.2%）判红；尺寸不一致直接判红；基线缺失
或多余组合也判红（防新增表面忘采、删面留死基线）。

## 已知边界

- **合成环境真实呈现**：data 页「统计生成于 Invalid Date」为合成壳无统计
  时间戳的真实 0.1.0 呈现（非产品缺陷），已随基线钉住；若产品修复该呈现，
  走上述更新流程即可。
- **跨 OS 字体栅格**：基线在 Windows 采集（中文栈=Microsoft YaHei）。Linux
  渲染的字体像素不同，跨 OS 比对会成片报警——跨平台启用需按 OS 分基线目录
  （parking lot，暂无需求）。
- **CI**：例行 PR CI 未安装 playwright，包装器自动 SKIP（不拖垮全量）；
  要在 CI 启用，加两步（`npm install` + `npx playwright install chromium`，
  均在 `tests/visual-baseline` 下）即可，harness 无 GUI 依赖。
