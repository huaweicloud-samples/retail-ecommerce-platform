# 部署指南

本文给出从零到可验证部署的完整命令，每一步都附预期输出。
命令默认在本仓库根目录执行，需要进入子目录时会显式写出 `cd`。

架构与设计取舍见 [architecture.md](architecture.md)。

## 0. 约定与占位符

| 占位符 | 含义 | 取值方式 |
|-|-|-|
| `<HW_ACCESS_KEY>` | Access Key ID | 控制台「我的凭证」页面的访问密钥区域 |
| `<HW_SECRET_KEY>` | Secret Access Key | 同上；两者只填入当前 shell，不要写入代码或提交到仓库 |
| `<APIG 专享实例 ID>` | 已就绪的网关实例 | 网关控制台的实例列表 |
| `<数据面地址>` | 文档数据服务的数据面地址 | 由实例详情页获取 |

## 1. 自检本地工具

```bash
python --version
terraform version
```

预期输出（版本不低于下表要求即可）：

```text
Python 3.11.9
Terraform v1.9.8
```

要求：Terraform CLI `>= 1.3`，huaweicloud Provider `>= 1.95`，Python 3.9 及以上。

## 2. 本地单元测试（部署前可做）

```bash
cd src
python -m pytest tests -q
cd ..
```

预期输出：

```text
4 passed in 0.02s
```

测试覆盖订单状态机、事件去重与业务规则，全部使用内存实现，不访问云端。

## 3. 生成受控令牌（按需）

本地或云端验证需要一个受控令牌：

```bash
python src/scripts/make_test_jwt.py --sub user-1 --roles customer
```

预期输出一个令牌字符串，并同时写入 `src/fixtures/test.jwt.json`。
脚本用 `jwt_secret` 的默认值签名；改用其它密钥时以 `TF_VAR_jwt_secret` 保持一致。

## 4. 静态校验

```bash
cd infra
terraform init
terraform validate
```

预期输出：

```text
Success! The configuration is valid, but there were some
validation warnings as shown above.
```

未使用官方插件镜像时，警告内容是本地开发覆盖的提示，属预期输出。

## 5. 配置访问凭据

```bash
export HW_ACCESS_KEY=<HW_ACCESS_KEY>
export HW_SECRET_KEY=<HW_SECRET_KEY>
```

## 6. 提供部署参数

分两种模式。**默认 `memory` 模式无需任何数据面配置**，可直接部署验证业务逻辑；
切到 `dynamodb` 模式才需要数据面参数。

```bash
# 入口
export TF_VAR_shared_apig_instance_id="<APIG 专享实例 ID>"
export TF_VAR_shared_apig_env_id="<共享环境 ID>"

# 仅 dynamodb 模式需要
# export TF_VAR_repository_mode="dynamodb"
# export TF_VAR_geminidb_endpoint="<数据面地址>"
# export TF_VAR_geminidb_access_key="<数据面访问密钥 ID>"
# export TF_VAR_geminidb_secret_key="<数据面访问密钥 Secret>"

# 令牌密钥
export TF_VAR_jwt_secret="<令牌签名密钥>"
```

### 6.1 主要参数

| 变量 | 类型 | 默认值 | 必填 | 说明 |
|-|-|-|-|-|
| `region` | string | `cn-north-4` | 否 | 固定为 `cn-north-4`（带校验） |
| `app_name` | string | `retail-ecommerce-platform` | 否 | 资源名称前缀 |
| `namespace` | string | `retail-ecommerce-platform` | 否 | 数据与事件命名空间 |
| `shared_apig_instance_id` | string | 空 | 建议 | 共享网关实例 ID；与 `shared_apig_env_id` 成对 |
| `shared_apig_env_id` | string | 空 | 建议 | 共享环境 ID；与 `shared_apig_instance_id` 成对 |
| `network_cidr` / `subnet_cidr` | string | `192.168.0.0/16` / `192.168.10.0/24` | 否 | 独立部署时的网络网段 |
| `shared_network_id` / `shared_subnet_id` | string | 空 | 按需 | 挂接共享私网；成对提供 |
| `repository_mode` | string | `memory` | 否 | `memory` 或 `dynamodb` |
| `geminidb_endpoint` / `geminidb_port` / `geminidb_protocol` / `geminidb_region` | — | 空 / `8000` / `https` / `cn-north-4` | dynamodb 模式必填 | 数据面连接与签名 |
| `geminidb_access_key` / `geminidb_secret_key` | string | 空 | dynamodb 模式必填 | 数据面凭据（敏感） |
| `geminidb_product_table` / `_order_table` / `_payment_table` / `_idempotency_table` | string | 见 `infra/variables.tf` | 否 | 四张逻辑表名 |
| `jwt_secret` | string | 本地默认值 | 建议 | 令牌签名密钥（敏感） |
| `jwt_issuer` / `jwt_audience` | string | 见 `infra/variables.tf` | 否 | 令牌签发者与受众 |
| `eg_source_id` / `eg_target_id` | string | 固定 UUID | 否 | 订阅标识；**每个部署需保持唯一** |
| `dcs_endpoint` / `css_endpoint` | string | 空 | 否 | 仅作健康信息，不参与业务闭环 |

### 6.2 几处容易填错的位置

- `eg_source_id` 与 `eg_target_id` 用于构造订阅标识，多个部署共用同一份默认值会互相干扰，
  多环境部署时必须各自覆盖。
- `repository_mode` 默认为 `memory`，重启或冷启动后数据不保留，仅用于验证。
- 数据面表的默认协议为 `https`，与 HTTP 端点对接时需显式改为 `http`。
- `jwt_secret` 的默认值仅供本地验证，生产必须覆盖。

## 7. 初始化数据表（仅 dynamodb 模式）

```bash
python src/scripts/bootstrap_geminidb.py
```

脚本按 `geminidb_*_table` 的取值创建四张表并写入样例商品，幂等可重复执行。
`memory` 模式下跳过本步。

## 8. 创建资源

```bash
cd infra
terraform plan -out=retail.tfplan
terraform apply retail.tfplan
```

`terraform plan` 预期会创建以下资源（复用共享网关时）：

```text
Plan: 17 to add, 0 to change, 0 to destroy.
```

构成：1 个日志组与 2 条日志流、2 个函数、1 个分组、1 条 API、1 条发布记录、
1 个事件通道与 1 个事件源与 1 条订阅、1 个委托。

自建网关实例时另加 1 个网络、1 个子网、1 个安全组与 1 个网关实例，并把环境一并创建。

`terraform apply` 完成后预期输出：

```text
Apply complete! Resources: 17 added, 0 changed, 0 destroyed.
```

## 9. 读取部署输出

```bash
terraform output
```

预期列出以下输出名：

```text
api_base_url          api_function_name     api_function_urn
api_invoke_url        api_log_stream_id     apig_env_id
apig_instance_id      consumer_function_name                      consumer_function_urn
consumer_log_stream_id                      data_namespace
event_channel_id      event_source_name     event_subscription_id
log_group_id          repository_mode
```

常用取值：

```bash
terraform output api_invoke_url       # REST 入口根地址
terraform output repository_mode      # 当前数据访问模式
terraform output event_channel_id     # 事件通道 ID
```

## 10. 验证部署

```bash
python src/scripts/verify_e2e.py
```

脚本对真实网关地址做端到端校验并把结果写入 `verification-result.json`。

## 11. 手工验证示例

```bash
export BASE="$(terraform output -raw api_invoke_url)"
export TOKEN="<由 src/scripts/make_test_jwt.py 生成的令牌>"
```

健康检查（不需要令牌）：

```bash
curl -s "$BASE/health"
```

预期返回 200。

创建订单：

```bash
curl -s -X POST "$BASE/orders" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"items":[{"productId":"p-1","quantity":2}]}'
```

预期返回 201 与订单，状态为 `Created`：

```json
{"orderId":"<订单标识>","userId":"user-1","status":"Created","total":198}
```

查询订单与商品：

```bash
curl -s "$BASE/orders/<订单标识>" -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/products" -H "Authorization: Bearer $TOKEN"
```

支付授权（幂等）：

```bash
curl -s -X POST "$BASE/payments" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"orderId":"<订单标识>","amount":198}'
```

预期返回授权结果；用同一订单再次提交不会重复授权。

### 11.1 验证事件驱动的状态推进

下单成功后接口会向事件通道发布订单事件，消费函数按类型推进状态。验证方式：

```bash
terraform output consumer_log_stream_id
```

在云日志服务控制台按该日志流检索，预期能看到消费记录，包含事件标识与处理结果
（`processed` / `duplicate` / `ignored`）。重复投递同一事件会返回 `duplicate`。

### 11.2 验证用户边界

用另一个 `sub` 的令牌读取他人的订单，预期返回 404：

```bash
export OTHER="<另一个 sub 的令牌>"
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/orders/<他人的订单标识>" -H "Authorization: Bearer $OTHER"
```

**部署完成后请按本节步骤在真实环境自行验证，确认功能可用后方可视为交付完成。**

## 12. 更新函数代码

接口函数与消费函数都以单文件内联部署，代码变化会触发对应函数更新：

```bash
cd infra
terraform apply
```

## 13. 释放资源

```bash
cd infra
terraform destroy
```

预期输出：

```text
Destroy complete! Resources: 17 destroyed.
```

销毁会删除两个函数、分组/环境/API/发布、事件通道与订阅、日志组与日志流、委托。
数据面实例与网关专享实例属共享资源，不在本目录的清理范围。

## 14. 故障排查

| 现象 | 定位方向 |
|-|-|
| 函数报「找不到入口」 | 入口必须为 `index.handler`；两个函数目录各自有 `index.py` |
| 接口返回 401 | 令牌缺失、签名密钥不一致或已过期 |
| 接口返回 404 且路径存在 | 检查路由分发的前缀是否与请求路径一致 |
| 下单后状态未推进 | 订单事件是否发布成功；订阅目标是否为消费函数；授权传播需时间 |
| 重复事件被处理了两次 | 去重依赖事件标识与「事件类型 + 订单标识」操作键，检查事件是否缺 `id` |
| `dynamodb` 模式返回 502 | 数据面地址、协议、端口、签名区域与凭据逐项核对 |
| `memory` 模式数据消失 | 属预期：内存模式不跨调用保留数据，仅供验证 |
| 日志为空 | 两个函数必须同时绑定日志组 ID/名称与日志流 ID/名称四个参数 |
