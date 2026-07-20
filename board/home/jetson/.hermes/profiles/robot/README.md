# Hermes robot profile 出厂资产

`deploy_board.sh` 会把本目录 rsync 到板上 `~/.hermes/profiles/robot/`。
这里只放可再生的出厂配置:

- `SOUL.md` — 龙虾人格与守则(注入系统提示)
- `config.yaml` — 模型 provider、工具裁剪、vlm/drive 两个 MCP 挂载

**不入库**(重刷板子后需手工恢复):`.env`(DEEPSEEK_API_KEY、API_SERVER_KEY,
600 权限)以及全部运行时状态(sessions/memories/state.db/cache 等)。
用户级 systemd 单元:`hermes-gateway-robot.service` 在
`board/home/jetson/.config/systemd/user/`;`voice-daemon`/`vlm-daemon`/`llama-server`
的单元随各自模块走(`voice/systemd/`、`vlm/systemd/`)。装好后
`systemctl --user enable --now hermes-gateway-robot llama-server vlm-daemon voice-daemon`。
