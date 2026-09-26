"""图片 / PDF 文字水印工具 —— 核心包。

关注点分层：
    spec    : 数据模型（参数 + 夹紧 + 序列化）
    fonts   : 系统字体发现与缓存
    layout  : 版式权威（块几何 / 平铺网格），图片与 PDF 共用；平铺是唯一版式
    render  : 两条渲染路径（Pillow 图片 / PyMuPDF 文档）
    media   : 文件类型识别、页面尺寸读取、输出路径规划
    ui      : Tkinter 界面
"""

#: 版本号**唯一真源**：打包脚本（``packaging/spec_common.APP_VERSION``）、使用
#: 说明、发布说明全部从这里读。发版只改这一处。
__version__ = "1.0.2"

__all__ = ["spec", "fonts", "layout", "render", "media"]
