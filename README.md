# google-tasks-cli

Google Tasks を端末から操作する小さな CLI (`gtasks`) です。Claude Code などの AI エージェントから使うことを主な目的にしていて、結果を JSON で出します。

- Python 3.9 以上の標準ライブラリだけで動きます (追加のパッケージは要りません)
- 要求する権限は Google Tasks (`https://www.googleapis.com/auth/tasks`) だけです
- 認証情報はリポジトリの外 (`~/.config/gtasks/`) に、自分だけが読める権限で置きます

```console
$ gtasks list --all-lists --text
[ ] 論文の査読  (期限 2026-10-20)  [仕事]  dGhpcyBpcyBh...
    [ ] 3 章を読む  [仕事]  YW4gZXhhbXBs...
[ ] 植物に水をやる  [家]  ZmFrZSBpZA...
```

## 準備

### 1. Google Cloud で OAuth クライアントを作る

自分専用のアプリとして、Google Cloud に登録します。最初の一度だけです。

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作る (名前は何でもよい)。
2. 「API とサービス」→「ライブラリ」で **Google Tasks API** を探して有効にする。
3. 「Google Auth Platform」(旧「OAuth 同意画面」) を開いて設定する。
   - アプリ名とサポートメールを入れる。対象 (User Type) は「外部」を選ぶ。
   - 「対象」でテストユーザーに自分のアカウントを足す。
   - **「アプリを公開」して「本番環境」にしておく。** 「テスト」のままだと、リフレッシュトークンが 7 日で切れ、毎週認可し直すことになります。自分だけで使うなら審査は要りません (認可のときに「Google で確認されていないアプリ」と警告が出るだけです)。
4. 「クライアント」で OAuth クライアント ID を作る。種類は **「デスクトップアプリ」** にする。
5. できたクライアントの JSON をダウンロードし、`~/.config/gtasks/client_secret.json` として置く。

```sh
mkdir -p -m 700 ~/.config/gtasks
mv ~/Downloads/client_secret_*.json ~/.config/gtasks/client_secret.json
chmod 600 ~/.config/gtasks/client_secret.json
```

置き場所は環境変数 `GTASKS_CONFIG_DIR` で変えられます (既定は `$XDG_CONFIG_HOME/gtasks`、なければ `~/.config/gtasks`)。

### 2. コマンドを PATH に置く

```sh
ln -s "$PWD/gtasks.py" ~/.local/bin/gtasks
```

### 3. 認可

ブラウザの開けない環境 (コンテナや SSH 先) でも使えるように、URL のコピーで進めます。

```sh
gtasks auth-url        # 認可用の URL が出る
```

1. 出た URL をブラウザで開き、アカウントを選んで許可する (「確認されていないアプリ」の警告が出たら「詳細」→「移動」)。
2. 最後に `http://127.0.0.1:8765/?state=...&code=...` へ飛ばされ、「接続できません」のような画面になります。**それで正常です。** アドレスバーの URL を丸ごとコピーする。
3. それを渡す。

```sh
gtasks auth-finish 'http://127.0.0.1:8765/?state=...&code=...&scope=...'
```

`~/.config/gtasks/token.json` ができれば完了です。以後はアクセストークンを自動で取り直します。

## 使い方

```sh
gtasks lists                                   # タスクリストの一覧
gtasks list                                    # 既定のリストの未完了タスク
gtasks list -l 仕事                            # リストは名前か ID で指定
gtasks list --all-lists                        # 全リストから集める
gtasks list --all-lists --due-by 2026-10-16    # 期限がその日以前のもの
gtasks list --completed                        # 完了したものも含める
gtasks list --completed-since 2026-10-16       # その日以降に完了したものを含める

gtasks add "論文の査読" -l 仕事 -n "メモ" -d 2026-10-20
gtasks add "3 章を読む" -l 仕事 -p 親のTASK_ID  # サブタスク
gtasks update TASK_ID -l 仕事 --title "新しい名前" -d 2026-10-21
gtasks update TASK_ID -l 仕事 --no-due          # 期限を外す
gtasks done TASK_ID -l 仕事
gtasks undone TASK_ID -l 仕事
gtasks delete TASK_ID -l 仕事
```

既定では JSON を出します。`-t` (`--text`) を付けると人向けの表示になります。

## Google Tasks の制限

CLI ではなく、Google Tasks 自体の仕様です。

- 期限は日付だけです。時刻を渡しても保存されません。
- 優先度・タグ・担当者はありません。
- 画面で設定した繰り返しは、API からは 1 件のタスクとして見えます。

## Claude Code から使う

`claude-skill/google-tasks/SKILL.md` は、Claude Code にこの CLI の使い方を教えるスキルです。

```sh
ln -s "$PWD/claude-skill/google-tasks" ~/.claude/skills/google-tasks
```

## テスト

Google の代わりにローカルの偽サーバを立てて試します。ネットワークにはつなぎません。

```sh
python3 -m unittest discover -s tests
```
