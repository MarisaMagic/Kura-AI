<p align="center">
  <a href="https://github.com/mizhexiaoxiao/vue-fastapi-admin">
    <img alt="Vue FastAPI Admin Logo" width="200" src="https://github.com/mizhexiaoxiao/vue-fastapi-admin/blob/main/deploy/sample-picture/logo.svg">
  </a>
</p>

<h1 align="center">MG-Agent</h1>

### 智能体中心

[](./deploy/sample-picture/agent-hub.png)

### 智能体对话

[](./deploy/sample-picture/agent-chat-1.png)

[](./deploy/sample-picture/agent-chat-2.png)

### 会话列表

[](./deploy/sample-picture/session-list.png)

### 本地启动
#### 后端
启动项目需要以下环境：
- Python 3.11

#### 使用 Pip 安装依赖
1. 创建虚拟环境
```sh
python3 -m venv venv
```

2. 激活虚拟环境
```sh
source venv/bin/activate  # Linux/Mac
# 或
.\venv\Scripts\activate  # Windows
```

3. 安装依赖
```sh
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

4. 启动服务
```sh
python run.py
```

服务现在应该正在运行，访问 http://localhost:9999/docs 查看API文档

#### 前端
启动项目需要以下环境：
- node v18.8.0+

1. 进入前端目录
```sh
cd web
```

2. 安装依赖(建议使用pnpm: https://pnpm.io/zh/installation)
```sh
npm i -g pnpm # 已安装可忽略
pnpm i # 或者 npm i
```

3. 启动
```sh
pnpm dev
```

### 目录说明

```
├── app                   // 应用程序目录
│   ├── api               // API接口目录
│   │   └── v1            // 版本1的API接口
│   │       ├── apis      // API相关接口
│   │       ├── base      // 基础信息接口
│   │       ├── menus     // 菜单相关接口
│   │       ├── roles     // 角色相关接口
│   │       └── users     // 用户相关接口
│   ├── controllers       // 控制器目录
│   ├── core              // 核心功能模块
│   ├── log               // 日志目录
│   ├── models            // 数据模型目录
│   ├── schemas           // 数据模式/结构定义
│   ├── settings          // 配置设置目录
│   └── utils             // 工具类目录
├── deploy                // 部署相关目录
│   └── sample-picture    // 示例图片目录
└── web                   // 前端网页目录
    ├── build             // 构建脚本和配置目录
    │   ├── config        // 构建配置
    │   ├── plugin        // 构建插件
    │   └── script        // 构建脚本
    ├── public            // 公共资源目录
    │   └── resource      // 公共资源文件
    ├── settings          // 前端项目配置
    └── src               // 源代码目录
        ├── api           // API接口定义
        ├── assets        // 静态资源目录
        │   ├── images    // 图片资源
        │   ├── js        // JavaScript文件
        │   └── svg       // SVG矢量图文件
        ├── components    // 组件目录
        │   ├── common    // 通用组件
        │   ├── icon      // 图标组件
        │   ├── page      // 页面组件
        │   ├── query-bar // 查询栏组件
        │   └── table     // 表格组件
        ├── composables   // 可组合式功能块
        ├── directives    // 指令目录
        ├── layout        // 布局目录
        │   └── components // 布局组件
        ├── router        // 路由目录
        │   ├── guard     // 路由守卫
        │   └── routes    // 路由定义
        ├── store         // 状态管理(pinia)
        │   └── modules   // 状态模块
        ├── styles        // 样式文件目录
        ├── utils         // 工具类目录
        │   ├── auth      // 认证相关工具
        │   ├── common    // 通用工具
        │   ├── http      // 封装axios
        │   └── storage   // 封装localStorage和sessionStorage
        └── views         // 视图/页面目录
            ├── error-page // 错误页面
            ├── login      // 登录页面
            ├── profile    // 个人资料页面
            ├── system     // 系统管理页面
            └── workbench  // 工作台页面
```
