---
name: google-tasks
description: Google Tasks のタスクを gtasks コマンドで読み書きする。ユーザーが Google Tasks・ToDo・タスクの確認、追加、完了、期限変更を頼んだときや、今日の作業一覧・日報を作るときに使う。
---

# Google Tasks (gtasks)

`gtasks` は Google Tasks API を呼ぶ自作の CLI。結果は JSON で標準出力に出る。失敗すると終了コード 1 で、理由が標準エラーに出る。

## 読む

```sh
gtasks lists                              # リストの一覧 (id, title)
gtasks list --all-lists                   # 全リストの未完了タスク。各タスクに "list" (リスト名) が付く
gtasks list -l 仕事                       # リストは名前か ID で指定。省略すると既定のリスト
gtasks list --all-lists --due-by 2026-10-16   # 期限がその日以前のもの
gtasks list --all-lists --completed-since 2026-10-16  # その日以降に完了したものも含める (日報用)
```

タスクの項目: `id`, `title`, `notes`, `status` (`needsAction` / `completed`), `due` (YYYY-MM-DD), `completed`, `parent` (サブタスクのとき親の id)。並びは親のすぐ後に子が来る。

## 書く

タスクの操作には、そのタスクがあるリストを `-l` で渡す (省略すると既定のリスト)。

```sh
gtasks add "タイトル" -l 仕事 -n "メモ" -d 2026-10-20   # -p 親ID でサブタスクにする
gtasks update TASK_ID -l 仕事 --title "新しいタイトル" -n "メモ" -d 2026-10-21
gtasks update TASK_ID -l 仕事 --no-due
gtasks done TASK_ID -l 仕事
gtasks undone TASK_ID -l 仕事
gtasks delete TASK_ID -l 仕事
```

## 注意

- 追加・変更・完了・削除は、ユーザーが頼んだもの、またはユーザーが確かめた案だけを実行する。削除は取り消せないので、対象のタイトルを示して確認してから行う。
- 期限は日付だけ。時刻を付けたい予定は Google Calendar に入れる。
- 優先度・タグはない。込み入った内容は `notes` に書く。
- 画面で設定した繰り返しは API からは見えず、1 件のタスクとして見える。
- 「まだ認可されていません」「リフレッシュトークンが失効」と出たら、自分で認可を進めず、ユーザーに README の「認可」の手順を案内する。
