# 退役纸质批单的字符纠错与对账

业务资料使用虚构编号和合成叙述。文本保持来件拼写和措辞。各表是独立的来源记录。
TSV 文件采用 UTF-8，首行为列名。行首 id 是该表的稳定记录号，空格和标点属于原文，空单元格表示来件未填。
日期时间采用所在地本地时间。资料内注明为编号列表的字段用竖线分隔。

## 资料字段

- `materials/scans.tsv`。candidates 来自转写方，形如 0/O 的两字符均为原材料可能字形。fingerprint 由扫描服务填写相同纸单的指纹，可空。
- `materials/issue_ledger.tsv`。reference 保留登记方录入的发料号，unit 由登记方独立提供。
- `materials/physical.tsv`。paper 是已登记单据。state 按实物实际动作记录。
- `materials/transfer_limits.tsv`。纸单双方共用的计量约定和会计停止自动冲回的条件。
- `materials/stock_handoffs.tsv`。现场称量和领用回执，时点独立于纸单扫描或销账时间。

## 运行资料检查

使用 CPython 3.11.2 和标准库。资料检查仅读取输入。

```sh
python3 pack_check.py
python3 -m unittest -v
```

## 对账引擎

`reconcile/` 在资料表之上实现字符纠错与对账，全部为标准库、只读输入：

- `textmodel.py`：单号字符（O/0、I/1、L/1）、规格词大小写、计量缩写与标题/表格区域；形状归一化只用于同形聚类，不改账。
- `parsing.py`：把扫描文本解析成带字段角色的候选；区分逐位候选（`O/0I/1-7`）与整词候选（`OI/01-7`），盒按库管约定换算公斤。
- `papers.py`：按 fingerprint 聚合纸单，同纸转发不重复入库；复核员的原纸格确认只定向到该纸，不扩散到同形单号。
- `units.py`：库管/账务约定解析与单位双读法裁决——所有读法通向同一归属和余额才先销账。
- `engine.py`：同货主同规格域内消歧、容量保护（错录大数量不占其他未决单余额）、合批分法枚举、有关组并查集与整组重算。
- `physical.py` / `queries.py`：在库与领用回执约束下的冲回、差额待核、双计风险与仓管交接查询。

```sh
# 全量对账报告：匹配、待查格、有关组、冲回与双计
python3 -m reconcile.cli
# 查任一纸单（指纹或扫描 id）还需查什么、实物归哪批
python3 -m reconcile.cli --paper Fr44
# 处理外部资料目录（路径作参数，不依赖工作目录）
python3 -I -c "import sys; sys.path.insert(0, '.'); \
  from reconcile.cli import main; main(['--directory', '/path/to/materials'])"
```

