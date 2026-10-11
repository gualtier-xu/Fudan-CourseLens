#!/usr/bin/env bash
# 按精确身份清理发布彩排沙箱目录（N9-R 2026-09-25 配套资产）。
#
# 红线：只删下表列名的目录，绝不通配 sweep；不存在则跳过；真实受管根
# （%LOCALAPPDATA%\CourseLens）与仓库工作树永不触碰。
# 出处：docs/release-day-ops-pack.md §9；计时与断言证据见
# archive/.../top-model-results/product-night9-lane-r-release-rehearsal-20260925.md
set -u
TARGETS=(
  "/tmp/n9r-e2e"            # 端到端彩排沙箱（profile/install/shots/logs）
  "/tmp/n9r-repro"          # HEAD-iss 复现性实验（payload/重编译产物）
  "/tmp/n9r-update-stage"   # 更新链彩排 stage（rehearse --keep 产物）
  "/tmp/cl-smoke"           # smoke_installer.sh 工作区（构建+装机+日志）
  "/tmp/cl-firstday"        # rehearse_first_day.sh 彩排根
  "/tmp/n9r-bundle-clone"   # 灾备 bundle 试克隆
)
removed=0
for t in "${TARGETS[@]}"; do
  if [ -d "$t" ]; then
    rm -rf "$t" && { echo "removed: $t"; removed=$((removed+1)); }
  else
    echo "absent : $t"
  fi
done
echo "cleanup done: ${removed}/${#TARGETS[@]} removed"
