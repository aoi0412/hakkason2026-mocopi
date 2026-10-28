# mocopi 姿勢サポート（Docker）

姿勢モニターの Web 画面、プロフィール共有 API、PC・スマートフォン間の WebSocket 通知、mocopi UDP 受信を Docker で起動します。プロフィールは名前付きボリュームに保存されます。

## 起動

Docker Desktop を起動し、このリポジトリのルートで実行します。

```sh
docker compose up --build
```

ログに `PC操作画面: http://localhost:8000/` と `mocopi UDP受信を開始しました: 0.0.0.0:12351` が出たら起動しています。

Mac の Wi-Fi IPv4 アドレスは、ターミナルで `ipconfig getifaddr en0` を実行して確認できます。Mac とスマートフォンのブラウザーで `http://<MacのLAN側IPv4アドレス>:8000/` を開いてください。スマートフォンと Mac は同じローカルネットワークに接続します。

停止するには `Ctrl+C` を押すか、別ターミナルで次を実行します。

```sh
docker compose down
```

ログ確認：

```sh
docker compose logs -f
```

## mocopi データの接続

サーバーは UDP `12351` で mocopi データを受信し、ブラウザーへ WebSocket で中継します。送信機器から UDP 宛先を指定できる場合は、Mac の LAN IPv4 アドレス、ポート `12351`、`mocopi (UDP)` 形式を指定してください。

### Windows の mocopi PC アプリ

このコンテナは UDP `12351` の mocopi 形式受信機です。Windows の mocopi PC アプリを使う場合、アプリ側の出力がこの受信形式と宛先に対応している必要があります。アプリから直接 UDP `12351` に送れない場合は、mocopi Receiver Plugin 対応ソフトウェアまたは別の中継処理が必要です。

## 公開ポート

- `8000/tcp`: Web 画面、プロフィール API
- `8765/tcp`: WebSocket 中継
- `12351/udp`: mocopi モーション受信

LAN 内で使用してください。mocopi の UDP データは暗号化されません。
