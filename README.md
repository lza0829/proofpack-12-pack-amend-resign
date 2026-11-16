# 05-proofpack（核证匣）

Proofpack — 举证放行：采样抽取 → 证据打包 → 校验清单 → 放行/驳回闭环。

与 Feedstitch（多源归并）、Batchgate（作业编排）、Syncledger（双端对账）、Changecast（CDC 投影）主链路不同。夹具全本地，不连活库。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5500 |
| API | http://localhost:10500 |

## 主链

选举证对象 → 按固定种子采样 → 生成证据包（含指纹）→ 跑基础校验清单 → 全过才可放行，否则驳回并留痕

## 补证（补证台）

证据包生成后发现漏条时，可对**未放行**包发起补证，在**原批次允许的样本集合内**追加或替换条目；
补证后必须重算包指纹，并作废旧清单通过态；补证成功后须复检清单通过才可放行。

- **提交策略（互斥二选一，落库在补证单上，不是口头开关）**
  - 增量补证 `incremental`：保留原条目，追加新条目，版本号加一，旧指纹进历史（`superseded`）
  - 整包重签 `resign`：以补证后的完整条目集生成新指纹，旧包版本作废、仅审计可查（`voided`）
- **失败命运（互斥二选一）**
  - 失败整笔回滚 `rollback`：包指纹、条目与样本行恢复为补证前，不留半补证可放行态
  - 失败进入补证失败态 `failed_state`：禁止放行，须显式撤销补证或再次补证成功后才能继续
- **放行闸**：补证进行中禁止放行；已放行包禁止补证；补证成功后仅当前版本清单全过才可放行；
  放行时复核包指纹与条目摘要，防止「改条目不改指纹」。
- **互证**（`GET /api/packs/{id}/verify`）：补证前后条数、指纹历史（当前/历史/作废）、清单状态与放行闸逐项核对。

补证单状态：`draft` → `submitting` → `succeeded` / `failed`（可 `cancelled` 撤销）。
接口见 `backend/app/routers/amendments.py`，服务见 `backend/app/services/amendment_service.py`。

## 种子举证对象

- 门店日结抽检
- 渠道结算抽检
- 价目变更核证

底座内置基础校验：条数一致、键唯一、必填非空。影子试跑 / 规则版本表空挂，深逻辑留给后题。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 本地后端

```bash
cd backend
pip install -r requirements.txt
set DATA_DIR=%TEMP%\proofpack-data
uvicorn app.main:app --host 127.0.0.1 --port 10500
```

## 测试

```bash
cd backend
DATA_DIR=$(mktemp -d) pytest tests -q
```
