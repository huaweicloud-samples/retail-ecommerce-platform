data "huaweicloud_availability_zones" "retail" {}
data "huaweicloud_account" "retail" {}

locals {
  use_shared_apig    = var.shared_apig_instance_id != "" && var.shared_apig_env_id != ""
  network_configured = var.shared_network_id != "" && var.shared_subnet_id != ""
  apig_instance_id   = local.use_shared_apig ? var.shared_apig_instance_id : try(huaweicloud_apig_instance.retail[0].id, "")
  apig_env_id        = local.use_shared_apig ? var.shared_apig_env_id : try(huaweicloud_apig_environment.retail[0].id, "")
  api_agency_name    = "${var.app_name}-api-eg"
  eg_target_agency   = "${var.app_name}-eg-target"
  eg_endpoint        = "https://eg.${var.region}.myhuaweicloud.com"
}

# 独立使用时创建本应用的网络和 APIG 专享实例；集中验证时两者均跳过。
resource "huaweicloud_vpc" "retail" {
  count = local.use_shared_apig ? 0 : 1

  name = "${var.app_name}-network"
  cidr = var.network_cidr
}

resource "huaweicloud_vpc_subnet" "retail" {
  count = local.use_shared_apig ? 0 : 1

  name              = "${var.app_name}-subnet"
  cidr              = var.subnet_cidr
  gateway_ip        = cidrhost(var.subnet_cidr, 1)
  vpc_id            = huaweicloud_vpc.retail[0].id
  availability_zone = data.huaweicloud_availability_zones.retail.names[0]
}

resource "huaweicloud_networking_secgroup" "retail" {
  count = local.use_shared_apig ? 0 : 1

  name                 = "${var.app_name}-secgroup"
  description          = "Security group for the retail ecommerce APIG instance"
  delete_default_rules = true
}

resource "huaweicloud_apig_instance" "retail" {
  count = local.use_shared_apig ? 0 : 1

  name              = "${var.app_name}-instance"
  edition           = "BASIC"
  vpc_id            = huaweicloud_vpc.retail[0].id
  subnet_id         = huaweicloud_vpc_subnet.retail[0].id
  security_group_id = huaweicloud_networking_secgroup.retail[0].id
  bandwidth_size    = 5

  ingress_bandwidth_size          = 5
  ingress_bandwidth_charging_mode = "bandwidth"

  available_zones = [
    data.huaweicloud_availability_zones.retail.names[0],
  ]
}

resource "huaweicloud_apig_environment" "retail" {
  count = local.use_shared_apig ? 0 : 1

  instance_id = huaweicloud_apig_instance.retail[0].id
  name        = "prod"
  description = "Production environment for the retail ecommerce API"
}

resource "huaweicloud_lts_group" "retail" {
  group_name  = "${var.app_name}-logs"
  ttl_in_days = 7
}

resource "huaweicloud_lts_stream" "api" {
  group_id    = huaweicloud_lts_group.retail.id
  stream_name = "${var.app_name}-api"
}

resource "huaweicloud_lts_stream" "consumer" {
  group_id    = huaweicloud_lts_group.retail.id
  stream_name = "${var.app_name}-consumer"
}

# API ingress 用的云服务委托，使函数可以使用 X-Auth-Token 发布 EG 事件。
resource "huaweicloud_identity_agency" "api_eg" {
  name                  = local.api_agency_name
  description           = "Retail API function publishes CloudEvents to EG"
  delegated_domain_name = var.fgs_agency_principal
  duration              = "FOREVER"

  project_role {
    project = var.region
    roles   = ["EG FullAccess", "VPC FullAccess"]
  }
}

# EG 投递 FunctionGraph 目标用的模式专属委托。
resource "huaweicloud_identity_agency" "eg_target" {
  name                  = local.eg_target_agency
  description           = "EG invokes the retail ecommerce consumer function"
  delegated_domain_name = "op_svc_eg"
  duration              = "FOREVER"

  project_role {
    project = var.region
    roles   = ["FunctionGraph FullAccess"]
  }
}

resource "huaweicloud_fgs_function" "api" {
  name        = "${var.app_name}-api"
  app         = "default"
  handler     = "index.handler"
  memory_size = 128
  timeout     = 15
  runtime     = "Python3.10"
  code_type   = "inline"
  func_code   = base64encode(file("${path.module}/../src/api/index.py"))
  agency      = huaweicloud_identity_agency.api_eg.name
  vpc_id      = local.network_configured ? var.shared_network_id : null
  network_id  = local.network_configured ? var.shared_subnet_id : null

  enable_lts_log  = true
  log_group_id    = huaweicloud_lts_group.retail.id
  log_group_name  = huaweicloud_lts_group.retail.group_name
  log_stream_id   = huaweicloud_lts_stream.api.id
  log_stream_name = huaweicloud_lts_stream.api.stream_name

  user_data = jsonencode({
    APP_NAMESPACE       = var.namespace
    REPOSITORY_MODE     = var.repository_mode
    GEMINIDB_ENDPOINT   = var.geminidb_endpoint
    GEMINIDB_PORT       = tostring(var.geminidb_port)
    GEMINIDB_PROTOCOL   = lower(var.geminidb_protocol)
    GEMINIDB_ACCESS_KEY = var.geminidb_access_key
    GEMINIDB_SECRET_KEY = var.geminidb_secret_key
    GEMINIDB_REGION     = var.geminidb_region
    PRODUCT_TABLE       = var.geminidb_product_table
    ORDER_TABLE         = var.geminidb_order_table
    PAYMENT_TABLE       = var.geminidb_payment_table
    IDEMPOTENCY_TABLE   = var.geminidb_idempotency_table
    DCS_ENDPOINT        = var.dcs_endpoint
    CSS_ENDPOINT        = var.css_endpoint
    JWT_SECRET          = var.jwt_secret
    JWT_ISSUER          = var.jwt_issuer
    JWT_AUDIENCE        = var.jwt_audience
    EG_REGION           = var.region
    EG_PROJECT_ID       = data.huaweicloud_account.retail.current_project_id
    EG_CHANNEL_ID       = huaweicloud_eg_custom_event_channel.retail.id
    EG_SOURCE_NAME      = huaweicloud_eg_custom_event_source.retail.name
    EG_ENDPOINT         = local.eg_endpoint
  })
}

resource "huaweicloud_fgs_function" "consumer" {
  name        = "${var.app_name}-consumer"
  app         = "default"
  handler     = "index.handler"
  memory_size = 128
  timeout     = 15
  runtime     = "Python3.10"
  code_type   = "inline"
  func_code   = base64encode(file("${path.module}/../src/consumer/index.py"))
  agency      = huaweicloud_identity_agency.api_eg.name
  vpc_id      = local.network_configured ? var.shared_network_id : null
  network_id  = local.network_configured ? var.shared_subnet_id : null

  enable_lts_log  = true
  log_group_id    = huaweicloud_lts_group.retail.id
  log_group_name  = huaweicloud_lts_group.retail.group_name
  log_stream_id   = huaweicloud_lts_stream.consumer.id
  log_stream_name = huaweicloud_lts_stream.consumer.stream_name

  user_data = jsonencode({
    APP_NAMESPACE       = var.namespace
    REPOSITORY_MODE     = var.repository_mode
    GEMINIDB_ENDPOINT   = var.geminidb_endpoint
    GEMINIDB_PORT       = tostring(var.geminidb_port)
    GEMINIDB_PROTOCOL   = lower(var.geminidb_protocol)
    GEMINIDB_ACCESS_KEY = var.geminidb_access_key
    GEMINIDB_SECRET_KEY = var.geminidb_secret_key
    GEMINIDB_REGION     = var.geminidb_region
    PRODUCT_TABLE       = var.geminidb_product_table
    ORDER_TABLE         = var.geminidb_order_table
    PAYMENT_TABLE       = var.geminidb_payment_table
    IDEMPOTENCY_TABLE   = var.geminidb_idempotency_table
    EG_SOURCE_NAME      = huaweicloud_eg_custom_event_source.retail.name
  })
}

resource "huaweicloud_eg_custom_event_channel" "retail" {
  name        = "${var.app_name}-channel"
  description = "Event channel for the retail ecommerce fulfillment flow"
}

resource "huaweicloud_eg_custom_event_source" "retail" {
  channel_id  = huaweicloud_eg_custom_event_channel.retail.id
  name        = "${var.app_name}-events-source"
  type        = "APPLICATION"
  description = "Application source for retail ecommerce CloudEvents"
}

# EG 订阅内部 source/target ID 需要在销毁重建后保持唯一。
resource "random_uuid" "eg_source_id" {}
resource "random_uuid" "eg_target_id" {}

resource "huaweicloud_eg_event_subscription" "retail" {
  depends_on = [
    huaweicloud_identity_agency.eg_target,
    huaweicloud_eg_custom_event_source.retail,
  ]

  channel_id  = huaweicloud_eg_custom_event_channel.retail.id
  name        = "${var.app_name}-subscription"
  description = "Deliver retail ecommerce events to the consumer function"

  sources {
    id            = random_uuid.eg_source_id.result
    provider_type = "CUSTOM"
    name          = huaweicloud_eg_custom_event_source.retail.name
    filter_rule = jsonencode({
      source = [{
        op     = "StringIn"
        values = [huaweicloud_eg_custom_event_source.retail.name]
      }]
    })
  }

  targets {
    id            = random_uuid.eg_target_id.result
    provider_type = "OFFICIAL"
    name          = "HC.FunctionGraph"
    detail_name   = "detail"
    detail = jsonencode({
      urn         = trimsuffix(huaweicloud_fgs_function.consumer.urn, ":latest")
      invoke_type = "ASYNC"
      agency_name = local.eg_target_agency
    })
    transform = jsonencode({
      type  = "ORIGINAL"
      value = ""
    })
  }
}

resource "huaweicloud_apig_group" "retail" {
  instance_id           = local.apig_instance_id
  name                  = "${var.app_name}-group"
  description           = "REST API group for the retail ecommerce platform"
  domain_access_enabled = true
}

resource "huaweicloud_apig_api" "retail" {
  instance_id             = local.apig_instance_id
  group_id                = huaweicloud_apig_group.retail.id
  type                    = "Public"
  name                    = "${var.app_name}-api"
  request_protocol        = "HTTPS"
  request_method          = "ANY"
  request_path            = "/{proxy}"
  security_authentication = "NONE"
  matching                = "Prefix"
  content_type            = "application/json"
  cors                    = true

  func_graph {
    function_urn     = trimsuffix(huaweicloud_fgs_function.api.urn, ":latest")
    version          = "latest"
    network_type     = "V1"
    request_protocol = "HTTPS"
    timeout          = 15000
    invocation_type  = "sync"
  }
}

resource "huaweicloud_apig_api_publishment" "retail" {
  instance_id = local.apig_instance_id
  env_id      = local.apig_env_id
  api_id      = huaweicloud_apig_api.retail.id
  description = "Published by Terraform for the retail ecommerce platform"
}
