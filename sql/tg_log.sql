-- ============================================================
-- 数据库: tg_log
-- 用途:   Telegram 群组发言日志机器人
-- 编码:   utf8mb4 (支持 emoji 及所有 Unicode 字符)
-- 优化:   content_hash 使用 BINARY(32) 代替 CHAR(64)
--         upsert 使用条件更新，仅字段变化时才刷新 updated_at
-- ============================================================

CREATE DATABASE IF NOT EXISTS tg_log
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE tg_log;


-- ============================================================
-- 群组表
-- 记录机器人所在的群组信息
-- ============================================================
CREATE TABLE IF NOT EXISTS `groups` (
  `id`         BIGINT        NOT NULL           COMMENT 'Telegram chat_id（群组为负数）',
  `title`      VARCHAR(255)  NOT NULL DEFAULT '' COMMENT '群组名称',
  `username`   VARCHAR(128)           DEFAULT NULL COMMENT '群组用户名（可为空）',
  `is_active`  TINYINT(1)    NOT NULL DEFAULT 1  COMMENT '是否启用记录: 1=启用 0=停用',
  `joined_at`  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '机器人加入时间',
  `updated_at` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                             ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='群组信息表';


-- ============================================================
-- 用户表
-- 记录在群组中发过言的用户信息
-- ============================================================
CREATE TABLE IF NOT EXISTS `users` (
  `id`         BIGINT        NOT NULL           COMMENT 'Telegram user_id',
  `username`   VARCHAR(128)           DEFAULT NULL  COMMENT 'TG 用户名（@后面的部分，可为空）',
  `first_name` VARCHAR(128)  NOT NULL DEFAULT '' COMMENT '名',
  `last_name`  VARCHAR(128)           DEFAULT NULL  COMMENT '姓（可为空）',
  `is_bot`     TINYINT(1)    NOT NULL DEFAULT 0  COMMENT '是否为机器人: 1=是 0=否',
  `first_seen` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '首次出现时间',
  `updated_at` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                             ON UPDATE CURRENT_TIMESTAMP COMMENT '信息最后更新时间',
  PRIMARY KEY (`id`),
  INDEX `idx_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='用户信息表';


-- ============================================================
-- 消息主表
-- 仅记录正常消息，重复消息在入库前已被拦截不写入
-- content_hash 使用 BINARY(32) 存储 SHA256 二进制摘要
-- 相比 CHAR(64) 索引体积减半，查询更快
-- ============================================================
CREATE TABLE IF NOT EXISTS `messages` (
  `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `message_id`   INT             NOT NULL               COMMENT 'Telegram message_id（群内唯一）',
  `chat_id`      BIGINT          NOT NULL               COMMENT '所属群组 chat_id',
  `user_id`      BIGINT          NOT NULL               COMMENT '发言用户 user_id',
  `content`      TEXT                     DEFAULT NULL  COMMENT '消息文本内容',
  `content_hash` BINARY(32)               DEFAULT NULL  COMMENT 'SHA256 二进制摘要（32字节），索引体积比 CHAR(64) 减半',
  `created_at`   DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '消息发送时间（毫秒精度）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_msg`            (`chat_id`, `message_id`),
  INDEX `idx_chat_time`          (`chat_id`, `created_at`),
  INDEX `idx_user_time`          (`user_id`, `created_at`),
  INDEX `idx_chat_user_time`     (`chat_id`, `user_id`, `created_at`),
  INDEX `idx_hash`               (`content_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='消息记录主表（仅正常消息，重复消息不入库）';


-- ============================================================
-- 重复消息日志表
-- 记录每次触发重复检测并执行删除的事件
-- ============================================================
CREATE TABLE IF NOT EXISTS `duplicate_log` (
  `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `chat_id`      BIGINT          NOT NULL               COMMENT '所属群组 chat_id',
  `user_id`      BIGINT          NOT NULL DEFAULT 0     COMMENT '触发重复的用户 user_id',
  `content_hash` BINARY(32)      NOT NULL               COMMENT 'SHA256 二进制摘要（32字节）',
  `content`      TEXT            NOT NULL               COMMENT '重复消息内容快照',
  `repeat_count` INT             NOT NULL DEFAULT 0     COMMENT '触发时检测到的重复次数',
  `deleted_ids`  JSON                     DEFAULT NULL  COMMENT '在 Telegram 侧成功删除的 message_id 列表（JSON 数组）',
  `triggered_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '触发删除的时间',
  PRIMARY KEY (`id`),
  INDEX `idx_chat_user`  (`chat_id`, `user_id`),
  INDEX `idx_chat_hash`  (`chat_id`, `content_hash`),
  INDEX `idx_time`       (`triggered_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='重复消息处理日志表';


-- ============================================================
-- 外键约束（可选，按需开启）
-- 若业务上保证数据一致性，可注释掉以提升写入性能
-- ============================================================
ALTER TABLE `messages`
  ADD CONSTRAINT `fk_messages_chat`
    FOREIGN KEY (`chat_id`) REFERENCES `groups` (`id`)
    ON UPDATE CASCADE ON DELETE RESTRICT,
  ADD CONSTRAINT `fk_messages_user`
    FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
    ON UPDATE CASCADE ON DELETE RESTRICT;

ALTER TABLE `duplicate_log`
  ADD CONSTRAINT `fk_duplog_chat`
    FOREIGN KEY (`chat_id`) REFERENCES `groups` (`id`)
    ON UPDATE CASCADE ON DELETE RESTRICT,
  ADD CONSTRAINT `fk_duplog_user`
    FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
    ON UPDATE CASCADE ON DELETE RESTRICT;