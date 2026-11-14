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
