# 班次报码偏差核报

站端班前确定本次比对的已发行规程件和分类规约，班后只按所选试跑签发汇总。中心可在站库关闭后签报。V1 定版、录入、发行及双规程复盘沿用 README.md 的接口。站库和中心库仍调用各自原有 `initialize()` 入口，可重开已有库。

## 站端登记和出件

- `issue.freeze.register_shift(register, protocol_issue_id, rules, fallback)` 使用本站 `IssueRegister` 和已发行 V1 规程件的发行号，返回本站的班次号。登记时固定规程件身份、规约及试跑录入界线。
- `rules` 是有序的列表或元组，可为空。每条规则是三元列表或元组 `(类别名, 匹配方式, 码序列)`。匹配方式取 `prefix` 或 `contains`。码序列为非空列表或元组，元素为非空 UTF-8 文本码。按整码精确匹配，`prefix` 从记录第一码开始，`contains` 可从任一码开始并连续匹配。按规则顺序选择首个匹配，均未匹配归 `fallback`。类别名和兜底名均为非空 UTF-8 文本且互不重复。规约可包含模型中从未出现的码。
- `issue.freeze.issue_shift_summary(register, shift_id, run_ids)` 接收本班次选定的非空实跑号列表或元组，每号仅出现一次。成员须在登记后录入且属于所选规程件的修订。空报码和 `outside_protocol` 试跑均参与，相同报码的不同试跑各计一次。成功返回 `IssuedEnvelope`。同一班次成功固定后，新的固定请求抛 `ValueError`，补送通过 `issue.retrieve.retrieve_piece(register, issue_id)` 取原字节。成员资格和出件失败时不产生汇总件，班次仍可结班。后来新增的实跑不改变已固定的汇总。
- 编号沿用 README.md 的正整数规则。站端未知的规程发行号、班次号或实跑号抛 `KeyError`。非法规约、错件类型、错修订、登记前试跑或重复实跑号抛 `ValueError`。

## 汇总件与中心实收

汇总件为 V2，与 V1 共用按发站和发行号识别的发行编号空间。外层沿用 README.md 的六个键，`version` 为整数 `2`，`site` 和 `revision_id` 与规程父件一致，`issue_id` 是本次汇总的发行号。正文仍用排序键、无附加空白、保留 Unicode 的 UTF-8 JSON，摘要仍覆盖正文 UTF-8 字节。正文含以下字段。

| 字段 | 含义 |
| --- | --- |
| `shift_id` | 已登记的本站班次号 |
| `protocol_issue_id` | 本班次指定的 V1 规程发行号 |
| `protocol_body_sha256` | 该 V1 规程正文摘要，64 位小写十六进制 |
| `rules`, `fallback` | 固定规约，序列和规则采用 JSON 数组 |
| `counts` | 按规则顺序加末尾兜底的非负整数计数数组，零计数也保留 |
| `total` | 正整数，等于 `counts` 之和 |

格式中只有上述字段。正文及外层均拒绝重复键，整数排除布尔。设备号、实跑号和原序列留站。中心用 `intake.receive.receive_piece(intake, raw)` 实收两类件，返回原 `AcceptedPiece`。汇总件可先于规程件实收，关联资格在签报时核对。格式、摘要或计数无效的件抛 `ValueError`，无成功实收。按原字节补送复领首执，同身份其他合法原件沿用 `PieceConflict` 及原冲突登记规则。

## 中心签报与领取

`review.publish.review_shift(reports, protocol_intake_id, summary_intake_id)` 按指定位置取实际实收的 V1 规程件和 V2 汇总件。汇总绑定的发站、规程发行号、修订和规程正文摘要均须匹配。即使正文相同，另一次发行也不能充作原件。缺件、错类或绑定不符抛 `ValueError`。V1 的 `review_pair` 继续只接收两份 V1 规程件。

期望数等于汇总的 `total` 乘该类的精确终止概率。公开边追加一码，静默边不追加码，只有实际结束的完整记录归类。计入空记录、同码分支及所有有限结束路径。按规则顺序加兜底，逐项返回实测数、期望数、实测减期望

签存对象公开 `report_id`、`shift_id`、`rows`、`parents`、`receipt`。`rows` 是有序元组，每行是 `(类别名, 实测整数, Fraction期望, Fraction差额)`。`parents` 为两个 `ReportParent` 的元组，规程在先，汇总在后。`receipt` 是固定字符串。签存失败不产生定稿、父件记录或报告回执，原实收可供重试。`review.history.read_shift_report(reports, report_id)` 按班次报告号直接读回签存对象，未知号抛 `KeyError`。中心重开和源端后续更正均不改变已签结果。

## 样例与运行

`samples/shift_review.json` 的 `models` 沿用原样例的规程声明字段。`shifts` 中每项分别登记为独立班次，在登记后录入 `runs`，按录入所得的试跑号结班。`expected` 每行依次为类别、实测、期望、差额，后两项用有理数字符串书写。前两个班次交换规则顺序。末个班次的规程沿两条同码分支生成 XXXY，只有从第二码开始才能匹配 XXY。Z 是规程外实跑，仍计入其观测类别。

保留原用例及 `python3 -B -m review.cli demo`。沿用仓库 `.pointwise-runtime.json` 的运行结构，让最终 `verify` 运行完整旧套件及班次离线核报断言，失败时退出非零。最终 `demo` 展示样例结班后凭实收件签出的类别差额及按原号领取的结果。
