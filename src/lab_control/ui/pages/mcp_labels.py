"""MCP operation and task state labels shared by Settings and the operation log."""

ACTION_LABELS = {
    "connect": "连接设备",
    "disconnect": "断开设备",
    "read_parameters": "读取参数",
    "apply": "修改参数",
    "acquire": "单次采集",
    "record": "连续记录",
    "monitor": "持续监测",
    "stop": "停止任务",
    "process": "计算指标",
}
STATE_LABELS = {
    "accepted": "已接受",
    "running": "运行中",
    "stopping": "正在收尾",
    "binary_saved": "原始数据已保存",
    "converting": "正在转换 CSV",
    "completed": "已完成",
    "partial": "部分完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}
