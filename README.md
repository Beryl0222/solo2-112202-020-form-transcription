# 退役纸质批单的字符纠错与对账

业务资料使用虚构编号和合成叙述。文本保持来件拼写和措辞。各表是独立的来源记录。
TSV 文件采用 UTF-8，首行为列名。行首 id 是该表的稳定记录号，空格和标点属于原文，空单元格表示来件未填。
日期时间采用所在地本地时间。资料内注明为编号列表的字段用竖线分隔。

## 资料字段

- `materials/scans.tsv`。candidates 来自转写方，形如 0/O 的两字符均为原材料可能字形。fingerprint 由扫描服务填写相同纸单的指纹，可空。
- `materials/issue_ledger.tsv`。reference 保留登记方录入的发料号，unit 由登记方独立提供。
- `materials/physical.tsv`。paper 是已登记单据。state 按实物实际动作记录。
- `materials/transfer_limits.tsv`。纸单双方共用的计量约定和会计停止自动冲回的条件。

## 运行资料检查

使用 CPython 3.11.2 和标准库。资料检查仅读取输入。

```sh
python3 pack_check.py
python3 -m unittest -v
```
