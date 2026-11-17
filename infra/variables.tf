variable "region" {
  description = "华为云区域。"
  type        = string
  default     = "cn-north-4"

  validation {
    condition     = var.region == "cn-north-4"
    error_message = "本示例的验证合同固定使用 cn-north-4。"
  }
}

variable "app_name" {
  description = "本应用所有资源的名称前缀。"
  type        = string
  default     = "retail-ecommerce-platform"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,50}$", var.app_name))
    error_message = "app_name 必须以小写字母开头，仅包含小写字母、数字和连字符。"
  }
}

variable "namespace" {
  description = "应用数据和事件命名空间。"
  type        = string
  default     = "retail-ecommerce-platform"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,50}$", var.namespace))
    error_message = "namespace 必须以小写字母开头，仅包含小写字母、数字和连字符。"
  }
}

variable "shared_apig_instance_id" {
  description = "共享 APIG 专享实例 ID。与 shared_apig_env_id 同时非空时复用共享实例。"
  type        = string
  default     = ""
}

variable "shared_apig_env_id" {
  description = "共享 APIG 环境 ID。与 shared_apig_instance_id 同时非空时复用共享环境。"
  type        = string
  default     = ""
}

variable "network_cidr" {
  description = "未复用共享 APIG 时创建的网络网段。"
  type        = string
  default     = "192.168.0.0/16"
}

variable "subnet_cidr" {
  description = "未复用共享 APIG 时创建的子网网段。"
  type        = string
  default     = "192.168.10.0/24"
}

variable "shared_network_id" {
  description = "共享 GeminiDB 私网所在网络 ID；与 shared_subnet_id 同时设置时挂接两个函数。"
  type        = string
  default     = ""
}

variable "shared_subnet_id" {
  description = "共享 GeminiDB 私网所在子网的 network_id；与 shared_network_id 同时设置时挂接两个函数。"
  type        = string
  default     = ""
}

variable "geminidb_endpoint" {
  description = "GeminiDB DynamoDB-compatible 私网主机或 IP，不含协议和端口时由 protocol/port 组合。"
  type        = string
  default     = ""
}

variable "geminidb_port" {
  description = "GeminiDB DynamoDB-compatible 数据面端口，默认使用私网端口 8000。"
  type        = number
  default     = 8000

  validation {
    condition     = var.geminidb_port >= 1 && var.geminidb_port <= 65535
    error_message = "geminidb_port 必须在 1 到 65535 之间。"
  }
}

variable "geminidb_protocol" {
  description = "GeminiDB 数据面协议，仅支持 HTTP 或 HTTPS。"
  type        = string
  default     = "https"

  validation {
    condition     = contains(["http", "https"], lower(var.geminidb_protocol))
    error_message = "geminidb_protocol 只能是 http 或 https。"
  }
}

variable "geminidb_access_key" {
  description = "GeminiDB DynamoDB-compatible AK，仅通过敏感变量或环境注入。"
  type        = string
  sensitive   = true
  default     = ""
}

variable "geminidb_secret_key" {
  description = "GeminiDB DynamoDB-compatible SK，仅通过敏感变量或环境注入。"
  type        = string
  sensitive   = true
  default     = ""
}

variable "geminidb_region" {
  description = "GeminiDB DynamoDB-compatible SigV4 区域。"
  type        = string
  default     = "cn-north-4"
}

variable "geminidb_product_table" {
  description = "产品表名或逻辑表标识。"
  type        = string
  default     = "retail_ecommerce_products"
}

variable "geminidb_order_table" {
  description = "订单表名或逻辑表标识。"
  type        = string
  default     = "retail_ecommerce_orders"
}

variable "geminidb_payment_table" {
  description = "支付幂等表名或逻辑表标识。"
  type        = string
  default     = "retail_ecommerce_payments"
}

variable "geminidb_idempotency_table" {
  description = "事件幂等表名或逻辑表标识。"
  type        = string
  default     = "retail_ecommerce_idempotency"
}

variable "repository_mode" {
  description = "数据访问模式：memory 保持本地闭环，dynamodb 显式启用 GeminiDB DynamoDB JSON + SigV4。"
  type        = string
  default     = "memory"

  validation {
    condition     = contains(["memory", "dynamodb"], var.repository_mode)
    error_message = "repository_mode 只能是 memory 或 dynamodb。"
  }
}

variable "dcs_endpoint" {
  description = "共享 DCS endpoint；当前仅注入健康信息，不在业务闭环中隐式连接。"
  type        = string
  default     = ""
}

variable "css_endpoint" {
  description = "共享 CSS endpoint；当前仅注入健康信息，不在业务闭环中隐式连接。"
  type        = string
  default     = ""
}

variable "jwt_secret" {
  description = "受控 HS256 JWT secret。仅用于开发验证，生产环境应从安全注入链路提供。"
  type        = string
  sensitive   = true
  default     = "retail-local-only-secret-v1"
}

variable "jwt_issuer" {
  description = "受控 JWT issuer。"
  type        = string
  default     = "retail-ecommerce-platform-local"
}

variable "jwt_audience" {
  description = "受控 JWT audience。"
  type        = string
  default     = "retail-ecommerce-platform-api"
}

variable "eg_source_id" {
  description = "本应用 EG 订阅 source UUID。每个 Terraform state 应保持唯一。"
  type        = string
  default     = "7a5e2c1d-1f12-4e11-9b30-1dc6c757c4d1"
}

variable "eg_target_id" {
  description = "本应用 EG 订阅 target UUID。每个 Terraform state 应保持唯一。"
  type        = string
  default     = "4fd0f7a2-8f31-45c5-8c42-9d9ed3fb8f7a"
}

variable "fgs_agency_principal" {
  description = "FunctionGraph 运行时委托主体。"
  type        = string
  default     = "op_svc_cff"
}
