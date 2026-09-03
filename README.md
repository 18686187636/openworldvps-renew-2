# 🚀 Openworld Free IPv6 VPS 自动续期脚本

基于 GitHub Actions 的 **Openworld Free IPv6 VPS** 全自动续期工具。采用 Playwright 自动化技术 + 智能 Discord OAuth 授权 + 多帧 GIF 动态验证码解析，实现无须人工干预的永久续期。

---

## 🌟 功能特性

- 🔑 **Discord OAuth 免干预登录**：利用账号的 `DISCORD_TOKEN` 向 Discord API 提交直接授权，跳过复杂的网页交互。
- 🧩 **多帧 GIF 算式验证码识别**：
  - 自动在浏览器上下文中获取 `blob:` 类型的多帧 GIF 动态验证码。
  - **拆帧 + 分区切割**：提取 GIF 的所有帧，将画面切割为左半区（数字A）、中区（运算符）、右半区（数字B）。
  - **模糊映射与跨帧投票**：清洗字符并映射误识别符号，利用跨帧概率统计得出高准确度的算式并自动计算结果。
- ⏱️ **智能天数检测**：自动解析面板当前的剩余到期天数，仅当剩余时间 `<= 5 天` 时才触发续期，避免无谓请求。
- 📢 **Telegram 结果通知**：可选配置 Telegram Bot，续期成功或失败时自动推送最新状态，**通知内容自动附带账号标识**（支持多账号区分）。
- 🏷️ **多账号友好**：通过环境变量 `ACCOUNT_NAME` 自定义账号名，所有 Telegram 消息开头均显示该名称，方便管理多个 VPS 实例。
- 📸 **自动保存验证码GIF**：自动保存验证码GIF，在 GitHub Actions 中保存为 Artifacts 便于排查。

---

## 🔐 GitHub Secrets 配置说明

在 GitHub 仓库依次点击 **Settings** ➔ **Secrets and variables** ➔ **Actions** ➔ **New repository secret** 配置以下变量：

| Secret 名称 | 是否必填 | 说明 |
| :--- | :---: | :--- |
| `DISCORD_TOKEN` | **必填** | 你的 Discord 账号授权 Token（获取方式见下文） |
| `TG_BOT_TOKEN` | ❌ 可选 | Telegram Bot Token（用于接收续期结果通知） |
| `TG_CHAT_ID` | ❌ 可选 | Telegram Chat ID（接收通知的用户或群组 ID） |
| `ACCOUNT_NAME` | ❌ 可选 | 自定义账号名称，会显示在 Telegram 通知中（如未设置则显示“未命名账号”），便于区分多账号 |

> [!NOTE]
> - 无需手动配置 VPS 地址，脚本登录后会**自动从面板检测**账号下所有 VPS 实例并逐一续期。
> - 若需同时管理多个 Openworld 账号，可在不同仓库（或工作流）中使用不同的 `ACCOUNT_NAME` 加以区分。

---

## 🛠️ GitHub Actions 部署指南

1. **Fork 本仓库** 到你自己的 GitHub 账号下。
2. **开启 Actions 权限**：在仓库的 **Actions** 标签页中点击按钮允许运行工作流。
3. **添加 Secrets**：在 **Settings ➔ Secrets and variables ➔ Actions** 中添加至少 `DISCORD_TOKEN`（如需 Telegram 通知和账号标识，可同时添加 `TG_BOT_TOKEN`、`TG_CHAT_ID` 和 `ACCOUNT_NAME`）。
4. **手动测试运行**：
   - 进入 **Actions** 标签页。
   - 选择左侧的 **Auto Renew Openworld VPS** 工作流。
   - 点击 **Run workflow** 按钮启动测试。
5. **定时自动运行**：工作流默认每 2 天自动触发运行一次，实现完全无人值守。

---

## 🔍 如何获取 Discord Token

1. 使用电脑浏览器打开 [Discord 网页版](https://discord.com/app) 并登录你的账号。
2. 按 `F12`（或 `Ctrl + Shift + I`）打开开发者工具。
3. 切换到 **网络 (Network)** 标签页。
4. 在 Discord 中点击任意频道或点击 Openworld Inc. 频道，触发 API 请求。
5. 在网络请求列表中点击任意 `discord.com/api/science` 开头的请求。
6. 在右侧 **请求标头 (Request Headers)** 中找到 `Authorization` 字段，该字段对应的长字符串即为 **DISCORD_TOKEN**。

> ⚠️ **安全提示**：请妥善保管你的 Discord Token，切勿泄露给他人。

---

## 📋 工作流配置示例（`.github/workflows/renew.yml`）

```yaml
name: Auto Renew Openworld VPS

on:
  schedule:
    - cron: '0 2 */2 * *'   # 每2天UTC 2点 (北京时间10点)
  workflow_dispatch:

jobs:
  Renew-openworld:
    runs-on: ubuntu-latest
    permissions:
      actions: write
      contents: read

    steps:
      - uses: actions/checkout@v4.2.2

      - name: ⚙ 设置 Python
        uses: actions/setup-python@v5.4.0
        with:
          python-version: '3.12'

      - name: 🛠️ 安装依赖
        run: |
          sudo apt-get update
          sudo apt-get install -y xvfb fonts-noto-cjk
          pip install playwright requests ddddocr Pillow numpy
          playwright install --with-deps chromium

      - name: 🚀 运行 Openworld 续期脚本
        env:
          DISCORD_TOKEN: ${{ secrets.DISCORD_TOKEN }}
          TG_BOT_TOKEN: ${{ secrets.TG_BOT_TOKEN }}
          TG_CHAT_ID: ${{ secrets.TG_CHAT_ID }}
          ACCOUNT_NAME: ${{ secrets.ACCOUNT_NAME }}   # 可选，用于通知显示账号名
          HEADLESS: "true"
        run: |
          xvfb-run --auto-servernum python3 apprenew.py

      - name: 💾 上传验证码 GIF
        if: always()
        uses: actions/upload-artifact@v5
        with:
          name: captcha-gifs
          path: |
            *.gif
          if-no-files-found: ignore
          retention-days: 3

      - name: 🧽 清理进程
        if: always()
        run: |
          pkill -f chromium || true
          pkill -f Xvfb || true

      - name: 🧹 清理旧的工作流运行记录
        if: always()
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          runs=$(gh run list --workflow="${{ github.workflow }}" --limit=100 --json databaseId --jq '.[].databaseId')
          count=0
          for run_id in $runs; do
            count=$((count + 1))
            if [ $count -gt 1 ]; then
              echo "删除运行记录: $run_id"
              gh run delete $run_id || true
            fi
          done
