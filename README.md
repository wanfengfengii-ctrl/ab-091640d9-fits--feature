# fits-cutout

天文巡检质控服务：从 FITS 主图像中提取小窗口，按头部标定值返回精确、可比较的物理量。

- 纯 Python 标准库实现（无第三方依赖），手工严格解析 FITS 结构
- 像素按 FITS 大端有符号整数解释；命中 `BLANK` 返回 `null`
- `BSCALE`/`BZERO` 用 `decimal.Decimal` 精确计算，输出无指数、无多余尾零的十进制字符串
- 可选 `integrity=required`：按 FITS 4.0 校验约定核对主 HDU 的 `CHECKSUM`/`DATASUM`，
  覆盖头部、数据及两端的 2880 字节填充
- 任何结构损坏、非有限标定值或非法窗口都返回明确的 4xx，绝不产生部分结果

## API

### `GET /health`

健康检查，返回 `200 {"status": "ok"}`。

### `POST /api/fits/cutout?x=&y=&width=&height=`

- 请求体：原始 FITS 文件，`Content-Type: application/fits`，不超过 16 MiB
- 查询参数：零基 `x`、`y`（≥ 0）与 `width`、`height`（≥ 1）；窗口不得越界，
  且 `width * height ≤ 10000`
- 可选查询参数 `integrity`：仅接受取值 `required`；省略时请求、响应与既有
  校验行为完全保持兼容，取值非法或重复时返回 400
- 仅接受单一主 HDU（`SIMPLE = T`，无扩展、无尾部内容）、`NAXIS = 2`、
  `BITPIX = 16` 或 `32`；校验 80 字符卡片、`END` 卡、2880 字节对齐、
  轴长度、数据长度与尾部填充

成功响应 `200`：

```json
{
  "x": 1,
  "y": 1,
  "width": 3,
  "height": 2,
  "sha256": "<原文件的 SHA-256>",
  "pixels": [["10.5", null, "10.7"], ["10.9", "11", "11.1"]]
}
```

`pixels` 按图像行序（y 递增）嵌套给出，每行内 x 递增；值为十进制字符串或 `null`。

#### 完整性校验：`integrity=required`

启用后，文件必须在主 HDU 头部**各恰好包含一张**格式合法的 `CHECKSUM` 与
`DATASUM` 卡（二者均为引号字符串，`CHECKSUM` 为 16 字符编码值）。服务在窗口
提取**之前**对上传的原始字节执行 FITS 4.0 反码和校验：

- `DATASUM` 必须等于数据段（含补到 2880 字节边界的零填充）的 32 位反码和；
- `CHECKSUM` 必须使整个 HDU（含头部卡、头部空格填充、数据数组与数据零填充）
  折叠求和为 `0xFFFFFFFF`——任一头卡、像素字节或填充字节被改写都会失配。

校验通过时成功响应沿用原字段并额外返回 `"integrityVerified": true`；
`sha256` 仍按上传字节计算，`BLANK`/`BSCALE`/`BZERO` 的输出语义不变。

下列情况一律返回明确的 `422`，且响应体只有 `error`，不含 `pixels` 或任何
部分窗口结果：校验字缺失、重复、格式非法，或任一受保护字节不匹配。

错误响应（均为 4xx，JSON `{"error": "..."}`）：

| 状态码 | 场景 |
| ------ | ---- |
| 400 | 参数缺失/非法、`integrity` 取值不是 `required`、窗口越界、像素数超限 |
| 413 | 文件超过 16 MiB |
| 415 | Content-Type 不是 `application/fits` |
| 422 | FITS 结构损坏、不受支持的 BITPIX/NAXIS、非有限标定值；`integrity=required` 时校验字缺失/重复/格式错误或字节不匹配 |

## 运行

```bash
# Docker Compose（宿主机端口由 HOST_PORT 环境变量配置，默认 8080）
HOST_PORT=9000 docker compose up app

# 或本地直接运行
PORT=8000 python -m fits_cutout.server
```

## 验证

一次性 `verify` 服务会等待 `app` 健康后依次执行：单元测试 → 构建（字节码编译）→
FITS 接口冒烟（含有效 `CHECKSUM`/`DATASUM` 校验冒烟与省略 `integrity` 的原请求
回归），并以退出码报告结果：

```bash
docker compose up --abort-on-container-exit --exit-code-from verify
echo $?   # 0 = 全部通过
```

本地等价命令：`sh verify/run.sh`（需服务已在 `APP_BASE_URL` 运行，默认
`http://127.0.0.1:8000`；仅跑单元测试可用 `python -m unittest discover -s tests -t . -v`）。

## 项目结构

```
fits_cutout/    # 服务源码：fits.py（严格解析+切图）、checksum.py（FITS 4.0 校验）、
                # server.py（HTTP API）
tests/          # 单元测试、FITS 构造工具与 data/ 下的 astropy 签名基准文件
verify/         # run.sh（测试+构建+冒烟）与 smoke.py
Dockerfile      # 单阶段镜像，构建期字节码编译校验
docker-compose.yml
```

`tests/data/*.fits` 由独立的 astropy 参考实现生成并签名，用于交叉验证本服务的
校验与编码逻辑（生成脚本不随镜像分发，运行期零第三方依赖）。
