# 快手关键词搜索 + 评论采集器

带 GUI 的快手 Web 采集工具：关键词搜索视频、逐视频抓评论，结果导出 CSV。
基于 PyQt5 + DrissionPage，搜索接口走纯算签名（Node 版 sig4 VM），不需要逆向浏览器环境。

## 功能

- 关键词搜索采集（`POST /rest/v/search/feed`，sig4 签名）
- 评论采集（`POST /rest/v/photo/comment/list`，免签名，自动翻页）
- 开始 / **停止采集**（停止后已采到的数据照样落盘）
- 打开浏览器 → 用户登录 → 程序自动读取 cookie 存到 `ks_config/cookies.json`，下次复用登录态
- 输出 CSV（utf-8-sig，Excel 直接打开不乱码）

## 本地运行

```bash
pip install -r requirements.txt
# 还需本机装有 Node.js（搜索签名用；打包后的 exe 自带，无需安装）
python ks_gui.py
```

## 打包 Windows EXE（GitHub Actions）

仓库已配好 `.github/workflows/build-exe.yml`：

1. 推代码到 GitHub
2. 打 tag 触发（或手动 **Actions → 打包 Windows EXE → Run workflow**）：

   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```

3. 打包完成后：
   - Actions 页面下载 `快手采集器-Windows-x64` 产物
   - 打 tag 时会自动创建 Release 并附上 zip

工作流做的事：准备 node.exe → 装依赖 → PyInstaller 打包（`ks_gui.spec`，onedir 模式）
→ 冒烟测试内置签名链路 → 压缩 → 上传产物。

> 用 onedir 而非 onefile：内置 node.exe 约 110MB，单文件模式每次启动都要解压上百 MB，很慢。

### 本地打包

```bash
pip install pyinstaller
# Windows: 把 node.exe 放到项目根目录（可选，不放则运行时用系统 node）
pyinstaller ks_gui.spec --noconfirm --clean
# 产物: dist/快手采集器/快手采集器.exe
```

## 目录结构

```
ks_gui.py       GUI 主程序（PyQt5）
sign.js         Node 签名入口（调用 sig4 VM）
jose.js         从页面抽取的 sig4 签名 VM
search.py       搜索接口命令行版
comment.py      评论接口命令行版
ks_gui.spec     PyInstaller 打包配置
ks_config/      cookie 存档 + chrome profile（已 gitignore，不入库）
```

## 注意

- 采集走的是纯 HTTP + 签名，**不需要登录也能搜**；登录只是为了拿更完整的 cookie。
- 浏览器统一 Chrome，自动化统一 DrissionPage；Chrome 路径会自动探测，也可在界面里手动指定。
- 接口偶发限流时返回 `result=2`（或没 result 字段），程序会自动退避重试，不是参数错误。

## 上传 GitHub 前自查

**必须上传**（缺一个打包就跑不起来）：

- `ks_gui.py`、`sign.js`、`jose.js`、`ks_gui.spec`、`requirements.txt`
- `.github/workflows/build-exe.yml` —— Action 只认**仓库根**下的这个路径

**不要上传**（已在 `.gitignore` 中）：

- `ks_config/` —— 含登录态 `cookies.json`
- `node.exe` —— CI 自己准备，别传这 110MB
- `*.csv`、`dist/`、`build/`

不要用网页拖拽上传，那样容易漏掉 `.github`、`.gitignore` 这类点开头的隐藏文件（漏了 Action 不生效）。推荐用 git：

```bash
cd 快手
git init -b main
git add .
git status          # 确认 ks_config/ 和 *.csv 没被加进来
git commit -m "快手采集器: PyQt5 GUI + Actions 打包 exe"
git remote add origin git@github.com:<你的账号>/<仓库名>.git
git push -u origin main
```

仓库先在 GitHub 网页建好（空仓库，不要勾 README / .gitignore）。私有仓库同样能跑 Action，
只是 Free 账号每月额度有限；公开仓库不限。
