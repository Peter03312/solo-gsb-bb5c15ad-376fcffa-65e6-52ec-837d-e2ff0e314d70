# 展馆多语展签审校 Web 应用

面向展馆的多语展签翻译—校对—发布流程系统。馆员维护中文原文与目标语言；译者逐条提交译文；另一名校对者确认；**同一账号不得确认自己的译文**。译文待审时，译者/校对者（提交者本人除外）可凭双修订号**认领 30 分钟**，认领期内仅认领人能确认。全部译文对应当前原文、全部确认后，馆员才能发布**不可变语言快照**；游客页只展示每种语言的最新已发布快照。

技术栈：**Python 3.12 / FastAPI + SQLite（WAL）+ 原生 HTML/CSS/JavaScript**，无前端构建步骤。

## 一键启动

```bash
docker compose up -d --build
```

首次启动自动：安装依赖、创建数据库表、初始化 3 个本地账号、2 种目标语言和 3 条示例中文展签。数据持久化在 Docker 命名卷 `exhibit-data` 中。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据库卷（慎用）
```

## 访问地址与端口

| 入口 | 地址 | 说明 |
|---|---|---|
| 登录 / 工作台 | http://localhost:8000/ | 已登录进入工作台，未登录跳转登录页 |
| 游客页（无需登录） | http://localhost:8000/visitor | 展示各语言最新已发布快照，未发布语言显示“暂无内容” |
| OpenAPI 文档 | http://localhost:8000/docs | 接口调试文档 |

端口映射在 `docker-compose.yml` 中为 `8000:8000`，如端口被占用可改成 `"8080:8000"`。

## 默认账号（本地账号）

| 角色 | 账号 | 密码 | 权限 |
|---|---|---|---|
| 馆员 librarian | `librarian` | `lib123` | 新增/修改中文原文、新增语言、发布语言快照 |
| 译者 translator | `translator` | `tra123` | 提交/修订译文、可确认**他人**译文 |
| 校对 reviewer | `reviewer` | `rev123` | 确认译文、也可提交/修订译文 |

> 译者与校对者都能提交译文和确认译文，二者形成交叉审校：**确认人 ≠ 提交人** 是硬性服务端校验，自己提交的译文必须换账号确认。演示“禁止自审”可用 `translator` 提交后，再换 `reviewer` 登录确认。

## 配置（环境变量）

在 `docker-compose.yml` 或启动前的环境 / `.env` 中配置，**仅在数据库首次初始化时生效**：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | `/data/exhibit.db` | SQLite 文件路径 |
| `SEED_LIBRARIAN_PW` | `lib123` | 馆员初始密码 |
| `SEED_TRANSLATOR_PW` | `tra123` | 译者初始密码 |
| `SEED_REVIEWER_PW` | `rev123` | 校对初始密码 |
| `CLAIM_TTL_SECONDS` | `1800` | 待审认领有效期秒数（默认 30 分钟；仅建议测试时改小） |

## 工作流程与修订号规则

1. **原文修订号** `source_rev` 从 1 起递增；**译文修订号** `trans_rev` 同样从 1 起递增。
2. **修改原文**（即使又改回原来的字句）：`source_rev +1`，该展签所有语言译文的确认立即失效，看板状态变为「过期」，须译者据新原文重新提交、再由他人确认。
3. **提交 / 修订译文**：`trans_rev +1`，确认清除（回到「待审」），并记录所对应的原文修订号。
4. **乐观并发控制**：提交译文须携带读取时的 `source_rev`（修订时还须携带 `trans_rev`）；确认须携带双修订号。服务端比对不一致即返回 `409` **拒绝并保留现状**，前端提示刷新重试。
5. 看板按语言分四类统计：**待译 / 待审 / 过期 / 已确认**，并实时给出不可发布原因清单（缺译文、译文过期、待确认等）。
6. **发布**：仅当该语言全部**在展**展签均有译文、译文对应当前原文修订号且全部已确认，馆员才能发布。快照带递增版本号，以 JSON 整体落库，系统不提供修改/删除接口——**一经发布即不可变**。之后原文/译文再变动，只影响下一版，游客页继续显示旧快照直到发布新版。
7. **撤展 / 复展**（详见下节）：馆员可按展签编号、读取时的**状态版本** `status_rev` 及原因撤展，或按编号 + 状态版本复展。撤展立即从**所有语言**的游客最新发布内容中隐藏该展签；历史快照与已登录用户查询快照的内容**保持不变**。撤展展签不计入语言看板的待办与发布条件，也不写入后续快照；**无在展展签时拒绝发布**。复展保留译文但清除该展签各语言确认，游客须等该语言译文按当前原文经他人重新确认并发布新快照后才能再次看到。
8. **待审认领**（本轮新增，详见下节）：译文处于「待审」时，译者或校对者可凭展签、语言及读取时的原文/译文双修订号**认领 30 分钟**；提交该译文的账号不得认领。认领期内其他账号确认一律 `409` 拒绝；无人认领时确认流程不变（仍只需双修订号）。到期、提前释放、原文/译文修订、撤展/复展、成功确认均使认领失效。

## 撤展 / 复展

### 规则要点

- 展签有独立于原文修订号的**状态版本** `status_rev`（从 1 起，撤展 / 复展各 +1），作为撤展、复展的乐观锁；修改原文只升 `source_rev`，不影响状态版本。
- **撤展立即对所有语言生效**：通过每语言的隐藏标记 `translations.public_hidden` 在游客接口中过滤实现，**不修改任何已发布快照 JSON**，因此：
  - 历史快照原样保留；已登录用户 `GET /api/snapshots/{id}` 的字段与内容完全不变；
  - 游客接口字段约定不变（仍是 `language / published / snapshot_id / version / published_at / entries`），只是 `entries` 中不再出现被撤展的展签；全部被隐藏时游客页显示“当前暂无在展展签”。
- 撤展展签从看板四类待办统计与发布条件中排除（单独出现在“已撤展展签”区，显示状态、原因、操作者、时间），也不会写入之后发布的新快照。
- 某语言没有任何在展展签时，发布返回 `409`，`detail.reasons` 中说明“当前没有在展展签（全部展签均已撤展），不能发布”。
- **复展**：保留各语言译文文本，清除该展签各语言的确认（`confirmed=0`）；隐藏标记在该语言**发布含该展签的新快照时才清除**，所以复展后、重新确认并发布前，游客仍看不到它（各语言相互独立）。
- 撤展期间禁止提交 / 修订 / 确认该展签译文（`409`）。
- 状态版本过期、重复变更（对已撤展再撤展 / 对在展再复展）、非馆员操作（`403`）、编号无效（`404`）、撤展原因为空（`422`）一律明确拒绝且不改变数据。
- 撤展 / 复展与原文修改、译文提交、确认、发布共用同一把写锁并在库内按当前状态校验，按一致状态串行处理。

### HTTP API

| 方法 | 路径 | 角色 | 请求体 | 说明 |
|---|---|---|---|---|
| POST | `/api/labels/{id}/withdraw` | librarian | `{"status_rev": 1, "reason": "文物外借展出"}` | 撤展；返回展签完整状态（含 `status_rev`、`withdrawn_by_name`、`withdrawn_at` 等） |
| POST | `/api/labels/{id}/restore` | librarian | `{"status_rev": 2}` | 复展；保留译文、清除各语言确认 |

错误码：`404` 编号无效；`403` 非馆员；`409` 状态版本过期或重复变更（响应体 `detail` 含服务器当前状态版本）；`422` 原因为空。看板 `GET /api/board/{lang_id}` 新增 `withdrawn_total` 与 `withdrawn_items[]`（其 `reason` 即撤展原因，`label` 内含 `withdrawn_reason / withdrawn_by_name / withdrawn_at / restored_by_name / restored_at`）；`GET /api/labels` 的每条展签也带这些字段。

### curl 调用示例

```bash
# 1) 馆员登录（保存 cookie）
curl -c lib.cookie -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"librarian","password":"lib123"}'

# 2) 查展签，取编号与当前状态版本
curl -b lib.cookie http://localhost:8000/api/labels

# 3) 撤展 #1（携带读取时的 status_rev 与原因）
curl -b lib.cookie -X POST http://localhost:8000/api/labels/1/withdraw \
  -H 'Content-Type: application/json' \
  -d '{"status_rev":1,"reason":"文物外借展出至年底"}'
# 游客两语言最新内容立即不再含 #1；历史快照不变
curl http://localhost:8000/api/public/languages

# 4) 复展 #1（携带撤展后的 status_rev=2）
curl -b lib.cookie -X POST http://localhost:8000/api/labels/1/restore \
  -H 'Content-Type: application/json' -d '{"status_rev":2}'

# 5) 译者/校对重新确认后，馆员发布新快照，游客才再次看到 #1
```

工作台操作：馆员在“中文原文展签”每条展签下填写撤展原因后点【撤展】，或对已撤展点【复展】；看板底部“已撤展展签”区展示原因、操作者与时间；译者 / 校对对撤展展签只读、无编辑操作。

## 待审认领（30 分钟）

### 规则要点

- **谁能认领**：`translator` 与 `reviewer` 均可；**提交该译文的账号不得认领自己的译文**（`403`，与“禁止自审”一致）；馆员只读看板，不能认领。
- **认领凭证**：展签编号、语言编号 + 读取时的**双修订号** `source_rev / trans_rev`。任一修订号与服务器当前值不一致即 `409` 拒绝且不改变状态（沿用乐观并发约定）。
- **可认领对象**：仅限在展、译文对应当前原文、未确认的「待审」译文。**已过期（基于旧版原文）、已确认、已撤展**译文的认领一律 `409` 拒绝且不改变状态；展签/语言/译文不存在返回 `404`。
- **有效期**：自认领起 **30 分钟**（可用环境变量 `CLAIM_TTL_SECONDS` 覆盖，便于测试）。
- **互斥**：同一译文（同一展签 × 语言）同一时刻仅一人持有有效认领：
  - 他人持有有效认领导致认领/确认均被拒绝（`409`，错误信息含认领人与到期时间）；
  - 本人重复认领 → 幂等返回原认领（**不续期、不改时间**）；
  - 到期或被提前释放/失效后，任何人（除提交者）可**接手**，到期时间从接手时刻重新计 30 分钟。
- **认领人可提前释放**；释放后他人可立即认领。仅认领人本人能释放（他人 `403`，无有效认领导致释放返回 `409`）。
- **认领与确认的关系**：
  - 有效认领期间，**仅认领人**可以确认该译文；其他账号确认 → `409` 且数据不变；
  - **无人有效认领时沿用现有确认流程**，确认请求仍只需原有双修订号，并保留“禁止自审（`403`）”和“修订冲突（`409`）”的拒绝约定；
  - **成功确认即结束认领**（`end_reason=confirmed`）。
- **认领失效时机**：原文修订、译文提交/修订（`trans_rev +1`）、撤展、复展均使该译文相关认领失效；过期版本无法认领。
- **并发一致性**：认领、释放、确认、译文提交、原文修改、撤展/复展、发布共用同一把写锁，在库内按当前修订号与展签状态校验，按一致状态串行处理。
- 认领记录保存在新表 `review_claims`（`UNIQUE(label_id, language_id)`：接手时覆盖同一行；`released_at` 非空或 `expires_at <= 当前时间` 即不再有效；`end_reason` 记录 `released / invalidated / confirmed`）。看板读取时实时判定，无需后台清理任务。

### HTTP API

| 方法 | 路径 | 角色 | 请求体 | 说明 |
|---|---|---|---|---|
| POST | `/api/board/{lang_id}/labels/{label_id}/claim` | translator/reviewer | `{"source_rev": 1, "trans_rev": 2}` | 认领 30 分钟；到期后可接手；返回认领记录（含 `claimer_id / claimer_name / claimed_at / expires_at / active / mine` 等） |
| POST | `/api/board/{lang_id}/labels/{label_id}/claim/release` | translator/reviewer | 无 | 认领人提前释放；返回已结束的认领记录 |

错误码：`404` 展签/语言/译文不存在（释放时还包括从无认领记录）；`403` 提交者认领自己的译文、非认领人释放、角色无权；`409` 撤展、双修订号过期、译文过期或已确认、他人持有有效认领、释放时无有效认领。

看板 `GET /api/board/{lang_id}` 的每个「待审」条目新增 `claim` 字段：无有效认领时为 `null`，否则为：

```json
{
  "id": 3,
  "label_id": 1,
  "language_id": 1,
  "source_rev": 2,
  "trans_rev": 2,
  "claimer_id": 3,
  "claimer_username": "reviewer",
  "claimer_name": "校对老王",
  "claimed_at": "2026-09-30 11:20:03",
  "expires_at": "2026-09-30 11:50:03",
  "released_at": null,
  "end_reason": null,
  "active": true,
  "mine": false
}
```

> 认领只影响审校流程；已发布快照与游客接口的字段、内容完全不变。

### curl 调用示例

```bash
# 0) 两名校对账号分别登录（提交译文的账号不得认领自己的译文）
curl -c tra.cookie -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"translator","password":"tra123"}'
curl -c rev.cookie -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"reviewer","password":"rev123"}'

# 1) 查看板，取待审条目的 source_rev / trans_rev（items[].claim 为 null 表示无人认领）
curl -b rev.cookie http://localhost:8000/api/board/1

# 2) 校对者凭双修订号认领展签 #1 的英文译文（30 分钟）
curl -b rev.cookie -X POST http://localhost:8000/api/board/1/labels/1/claim \
  -H 'Content-Type: application/json' \
  -d '{"source_rev":1,"trans_rev":1}'
# -> 200，返回 expires_at（约 30 分钟后）、active=true、mine=true

# 3) 认领人提前释放（其他账号随即可以认领/确认）
curl -b rev.cookie -X POST http://localhost:8000/api/board/1/labels/1/claim/release

# 4) 其他账号在认领有效期内确认 -> 409（错误信息含认领人与到期时间）
curl -b tra.cookie -X POST http://localhost:8000/api/board/1/labels/1/confirm \
  -H 'Content-Type: application/json' \
  -d '{"source_rev":1,"trans_rev":1}'

# 5) 无人认领时确认流程不变：仍只带双修订号
curl -b rev.cookie -X POST http://localhost:8000/api/board/1/labels/1/confirm \
  -H 'Content-Type: application/json' \
  -d '{"source_rev":1,"trans_rev":1}'
# 确认成功同时结束该译文的有效认领
```

工作台操作：语言看板每条「待审」译文下展示认领状态条——未认领时非提交者可见【认领 30 分钟】与【确认译文】按钮；持有认领时显示**到期倒计时**、【提前释放认领】，确认按钮照常可用；他人持有时提示认领人与到期时间、隐藏确认按钮。到期瞬间页面自动刷新一次，接手/确认按钮即时出现。馆员看板只读展示认领人与到期时间。

## 本地无 Docker 运行（开发）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./exhibit.db uvicorn app.main:app --reload
```

## 目录结构

```
app/
├── db.py                 # 连接/Schema/默认数据初始化（账号、语言、示例展签）
├── main.py               # FastAPI 全部接口、鉴权、看板聚合与发布逻辑
└── static/
    ├── login.html        # 登录页
    ├── app.html/app.js   # 审校工作台（按角色显示操作）
    ├── visitor.html      # 游客公开页
    └── styles.css
Dockerfile
docker-compose.yml
requirements.txt
```
