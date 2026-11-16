export const PACK_STATUS = {
  built: '已生成',
  checked_ok: '校验通过',
  checked_fail: '校验未过',
  released: '已放行',
  rejected: '已驳回',
  amend_failed: '补证失败',
}

export const AMEND_STATE = {
  none: '无补证',
  in_progress: '补证进行中',
  failed: '补证失败态',
}

export const AMEND_STATE_TAG = {
  none: '',
  in_progress: 'warn',
  failed: 'danger',
}

export const AMENDMENT_STATE = {
  draft: '草稿',
  submitting: '提交中',
  succeeded: '补证成功',
  failed: '补证失败',
  cancelled: '已撤销',
}

export const STRATEGY = {
  incremental: '增量补证',
  resign: '整包重签',
}

export const FAILURE_POLICY = {
  rollback: '失败整笔回滚',
  failed_state: '失败进入补证失败态',
}

export const HISTORY_STATE = {
  active: '当前生效',
  superseded: '历史（保留）',
  voided: '作废（仅审计）',
}

export function packStatusLabel(status) {
  return PACK_STATUS[status] || status
}

export function amendStateLabel(state) {
  return AMEND_STATE[state] || state
}
