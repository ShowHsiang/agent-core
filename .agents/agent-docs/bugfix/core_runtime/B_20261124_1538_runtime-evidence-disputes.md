---
id: B_20261124_1538_runtime-evidence-disputes
title: 同一版本事实的矛盾去重与有据关闭
type: B
author: ShowHsiang
author_email: xiangxiaoneng@h-partners.com
modules:
  - openjiuwen/core/single_agent
files:
  - openjiuwen/core/single_agent/runtime_monitor/schema.py
  - openjiuwen/core/single_agent/runtime_monitor/memory.py
  - tests/unit_tests/agent/react_agent/test_runtime_monitor.py
designs:
  - D-20261029-1613-react-runtime-intervention
---

# 同一版本事实的矛盾去重与有据关闭

两次真实调用依次读取同一装运版本的两份门位副本、两份封签副本和一份签署原件。主 Agent 最终给出了原件中的门位和封签颜色。旧监控把每个 JEV 矛盾证据对算成一项独立问题，原件与错误副本产生的新证据对让未解决数从 2 增至 4。一次运行首先列出了目录，两条轨迹在五次实际读取上的结果相同。

SDK 继续按证据对保存来源、首次发现轮次和判断依据。宿主可以提供对象、属性、作用域、版本和裁决引用。SDK 按相同事实归并未解决数，只有完整的同版本宿主裁决才能关闭这项分歧。竞争裁决使分歧重开，窗口按重开轮次计数。旧会话根据仍保留的证据恢复归属。无结构证据沿用按对统计。业务验收继续由独立的 Verification 提供。

两条已归档轨迹使用原工具响应及当时的 JEV 判断回放，五次读取旧值均为 `0,1,1,2,4`，新值均为 `0,1,1,2,0`。每条轨迹仍有四个原始证据对并记录裁决引用，已验证进展为 0。使用测试实例共享 JEV 重评五步得到同一计数，四对证据全部关闭。

首次网页验收中，主模型将四份副本放在同一批次。SDK 只与历史轮比较，遗漏两项当轮分歧。修复后同一轮的观测也作为后续观测的候选，以同一批 JEV 问题比较，每对最多比较一次。原有默认最多三对的限制继续生效。网页实测同时读取四份副本再取原件，两个工具轮的计数依次为 2、0。逐个读取时依次为 0、1、1、2、0。两种方式均送达 VERIFY 并观察到 improved。延后批量评估时，每一步只使用截至该轮的裁决。

独立低收益回归和真实共享 JEV 复验使用两个取得进展的探索动作和三个不同的无收益动作。默认五步收益为 `[1, 0.5, 0, 0, 0]`，均值 0.30，重复计数 1，仅第五轮触发 LOW_GAIN_STREAK。回归同时覆盖不完整原件、作用域不同、旧数据、竞争裁决和已送达建议的效果。
