# PT Tracker Manager

PT网站Tracker地址管理工具，用于存储、更新和管理各种PT网站的tracker信息。

## 功能特点

- 集中管理多个PT网站的tracker地址
- 分类存储不同类型的tracker（私有、半公开、公开）
- 提供tracker地址有效性验证功能（待开发）
- 简单易用的JSON格式存储
- 支持通过API方式获取和更新tracker信息（待开发）

## 已添加的Tracker

### 公开Tracker
- **OpenTrackr**: 流行的公开tracker，支持UDP和HTTP协议
- **Demonoid**: 知名的公开tracker，提供UDP协议支持
- **Popular Public Trackers**: 集合了多个流行的公开tracker，包括stealth.si、explodie.org等

### 半公开Tracker
- **Example Semi-Private**: 半公开tracker的示例格式

## 项目结构

```
├── trackers/
│   ├── private/          # 私有PT站点tracker（不包含实际内容）
│   │   └── README.md     # 私有tracker使用说明
│   ├── semi_private/     # 半公开PT站点tracker
│   │   ├── README.md     # 半公开tracker使用说明
│   │   └── example_semi_private.json  # 示例半公开tracker
│   └── public/           # 公开PT站点tracker
│       ├── README.md     # 公开tracker使用说明
│       ├── opentrackr.json    # OpenTrackr tracker信息
│       ├── demonoid.json      # Demonoid tracker信息
│       └── popular_trackers.json  # 流行公开tracker集合
├── utils/
│   └── validator.js      # tracker文件验证工具
├── .gitignore
├── package.json
└── README.md
```

## 使用方法

1. 克隆仓库：`git clone https://github.com/qingzhh/public-resources.git`，然后执行 `cd public-resources/projects/pt-tracker-manager`
2. 根据需要，从`trackers/public/`目录下获取公开tracker信息
3. 半公开tracker需要注册才能使用，请参考对应站点的注册规则
4. 私有tracker仅供特定用户使用，请确保您有合法访问权限

## JSON格式说明

每个tracker文件使用JSON格式，包含以下字段：
- `name`: Tracker名称
- `description`: Tracker描述
- `type`: Tracker类型（public/semi_private/private）
- `domain`: Tracker域名
- `announce`: 包含多个announce URL的数组
- `created_at`: 创建时间
- `updated_at`: 更新时间
- `status`: 状态（active/inactive）

半公开和私有tracker可能包含额外字段，如`requires_registration`、`invite_system`等。