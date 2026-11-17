# retail-ecommerce-platform

[![Status](https://img.shields.io/badge/Status-Incubating-blue)]()
[![Huawei Cloud](https://img.shields.io/badge/Huawei%20Cloud-Samples-red)]()
[![Scenario](https://img.shields.io/badge/Scenario-internet-success)](https://3ms.huawei.com/docs/docinfo/1300901382590492672?bookstackId=866760814559879168&gid=3591759&l=zh-cn&documentkind=&attachmentIdx=5)

事件驱动后端的关键不在「发事件」，而在三件事：**事件从哪来**、**消费怎么幂等**、
**一致性边界在哪**。这套实践把三件事各自固定下来：
- **事件从显式发布来**：订单写入成功后由接口显式发布事件，不依赖存储层的变更流。
事件类型就是业务动作（`OrderCreated`、`WarehouseCompleted`、`DeliveryCompleted`），
生产与消费的契约写在事件本身。
- **消费双重去重**：同时记录事件标识与「事件类型 + 订单标识」的操作键，
任一命中即视为重复，返回 `duplicate` 而不重复推进状态。
- **一致性用条件写与补偿**：订单、支付与幂等记录都把用户标识放进主键，
状态推进使用「当前状态匹配」的条件写，非法跃迁不会覆盖已有状态。
该边界不提供跨表原子提交，这一点在文档中显式保留。
- **数据访问双实现**：默认内存模式无需任何外部依赖即可验证业务闭环；
切到兼容接口模式只需改一个配置项，业务代码不变。
**解决的问题**：把「下单 → 支付 → 仓储 → 配送」这条链路做成可部署、可验证、可重放的最小闭环。
**适用于**：订单、履约、工单等需要有状态推进与异步解耦的业务；

## Overview

This repository is created from the huaweicloud-samples automated repository request workflow.

## Getting Started

Add setup, deployment, and verification steps here.

## Contributing

Please use pull requests and follow the repository review rules.

## License

This project is licensed under the MIT-0 license.

## Maintainers

CODEOWNERS: @Ferguson2211

## Feedback

Please use GitHub Issues: https://github.com/huaweicloud-samples/retail-ecommerce-platform/issues
