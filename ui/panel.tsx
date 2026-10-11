import {
  ActionButton,
  Card,
  DataTable,
  EmptyState,
  Grid,
  Page,
  RefreshButton,
  Stack,
  StatCard,
  Toolbar,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type MemoRow = { id: string; content: string; validity: string }
type TaskRow = { id: string; type: string; description: string; next_at: string }
type State = { memos?: MemoRow[]; tasks?: TaskRow[] }

/**
 * 速记备忘录 — 第一版简化面板：
 * 状态备忘 / 定时任务两张只读列表 + 每行删除按钮 + 刷新。
 * 新建/修改仍通过对话（LLM 工具）或插件入口完成。
 */
export default function Panel(props: PluginSurfaceProps<State>) {
  const { actions, state, t } = props
  const del = actions.find((action) => action.id === "delete_item") as HostedAction | undefined
  const memos = state.memos || []
  const tasks = state.tasks || []

  const colId = t("panel.col.id", { defaultValue: "ID" })
  const colActions = t("panel.col.actions", { defaultValue: "操作" })
  const empty = t("panel.empty", { defaultValue: "暂无条目" })
  const delLabel = t("panel.delete", { defaultValue: "删除" })

  const opCell = (row: { id: string }) =>
    del ? <ActionButton action={del} values={{ item_id: row.id }} label={delLabel} tone="danger" /> : null

  return (
    <Page
      title={props.plugin.name}
      subtitle={t("panel.subtitle", { defaultValue: "AI 自我备忘录与主动定时提醒" })}
    >
      <Stack gap={12}>
        <Grid cols={2} gap={12}>
          <StatCard
            label={t("panel.stat.memos", { defaultValue: "状态备忘" })}
            value={String(memos.length)}
          />
          <StatCard
            label={t("panel.stat.tasks", { defaultValue: "定时任务" })}
            value={String(tasks.length)}
          />
        </Grid>

        <Toolbar>
          <RefreshButton label={t("panel.refresh", { defaultValue: "刷新" })} tone="primary" />
        </Toolbar>

        <Card title={t("panel.memos.title", { defaultValue: "状态/人设备忘录" })}>
          {memos.length ? (
            <DataTable
              data={memos}
              rowKey="id"
              columns={[
                { key: "id", label: colId },
                {
                  key: "content",
                  label: t("panel.col.content", { defaultValue: "内容" }),
                },
                {
                  key: "validity",
                  label: t("panel.col.validity", { defaultValue: "有效期" }),
                },
                { key: "op", label: colActions, render: (row: MemoRow) => opCell(row) },
              ]}
            />
          ) : (
            <EmptyState title={empty} />
          )}
        </Card>

        <Card title={t("panel.tasks.title", { defaultValue: "定时提醒与周期计划" })}>
          {tasks.length ? (
            <DataTable
              data={tasks}
              rowKey="id"
              columns={[
                { key: "id", label: colId },
                { key: "type", label: t("panel.col.type", { defaultValue: "类型" }) },
                {
                  key: "description",
                  label: t("panel.col.description", { defaultValue: "描述" }),
                },
                {
                  key: "next_at",
                  label: t("panel.col.next", { defaultValue: "下一次触发" }),
                },
                { key: "op", label: colActions, render: (row: TaskRow) => opCell(row) },
              ]}
            />
          ) : (
            <EmptyState title={empty} />
          )}
        </Card>
      </Stack>
    </Page>
  )
}
