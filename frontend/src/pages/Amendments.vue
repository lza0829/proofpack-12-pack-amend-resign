<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getJSON, postJSON } from '../api'
import {
  AMENDMENT_STATE,
  AMEND_STATE_TAG,
  FAILURE_POLICY,
  HISTORY_STATE,
  STRATEGY,
  amendStateLabel,
  packStatusLabel,
} from '../labels'

const packs = ref([])
const packId = ref(null)
const pool = ref(null)
const amendments = ref([])
const verify = ref(null)
const draftStrategy = ref('incremental')
const draftFailure = ref('rollback')
const selectedAdd = ref([])
const replaceFrom = ref('')
const replaceTo = ref('')
const err = ref('')
const notice = ref('')
const busy = ref(false)
const showVerify = ref(false)

const selectedPack = computed(() => packs.value.find((p) => p.id === Number(packId.value)) || null)
const amendable = computed(
  () => selectedPack.value && selectedPack.value.status !== 'released',
)
const outOfPack = computed(() =>
  pool.value ? pool.value.allowed_pool.filter((x) => !x.in_pack) : [],
)
const inPack = computed(() =>
  pool.value ? pool.value.allowed_pool.filter((x) => x.in_pack) : [],
)
const currentAmendState = computed(() => selectedPack.value?.amend_state || 'none')

async function refreshPacks() {
  packs.value = await getJSON('/api/packs')
  if (!packId.value && packs.value.length) packId.value = packs.value[0].id
}

async function loadDetail() {
  if (!packId.value) return
  pool.value = await getJSON(`/api/packs/${packId.value}/pool`)
  amendments.value = await getJSON(`/api/amendments?pack_id=${packId.value}`)
  verify.value = null
  applyPreset()
}

onMounted(async () => {
  try {
    await refreshPacks()
    await loadDetail()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(packId, async () => {
  try {
    await loadDetail()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function applyPreset() {
  // 两种提交策略、两种失败命运互为互斥单选；改选时清掉不适用的操作残留
  selectedAdd.value = []
  replaceFrom.value = ''
  replaceTo.value = ''
}

async function createAndSubmit() {
  err.value = ''
  notice.value = ''
  const ops = []
  for (const key of selectedAdd.value) ops.push({ op: 'add', business_key: key })
  if (replaceFrom.value && replaceTo.value) {
    ops.push({
      op: 'replace',
      from_business_key: replaceFrom.value,
      business_key: replaceTo.value,
    })
  }
  if (!ops.length) {
    err.value = '请至少选择一条追加或替换操作'
    return
  }
  busy.value = true
  try {
    const created = await postJSON('/api/amendments', {
      pack_id: Number(packId.value),
      strategy: draftStrategy.value,
      failure_policy: draftFailure.value,
      ops,
    })
    notice.value = `补证单 #${created.id} 已发起，补证进行中禁止放行`
    const result = await postJSON(`/api/amendments/${created.id}/submit`)
    if (result.state === 'succeeded') {
      notice.value = `补证单 #${created.id} 补证成功：包指纹已重算，清单已复检通过`
    } else if (result.state === 'failed') {
      notice.value = `补证单 #${created.id} 补证失败：${result.failure_reason}`
    } else {
      notice.value = `补证单 #${created.id} 状态：${result.state}`
    }
    await refreshPacks()
    await loadDetail()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function cancelAmendment(a) {
  err.value = ''
  busy.value = true
  try {
    await postJSON(`/api/amendments/${a.id}/cancel`)
    notice.value = `补证单 #${a.id} 已撤销`
    await refreshPacks()
    await loadDetail()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function runVerify() {
  err.value = ''
  try {
    verify.value = await getJSON(`/api/packs/${packId.value}/verify`)
    showVerify.value = true
  } catch (e) {
    err.value = String(e.message || e)
  }
}
</script>

<template>
  <div class="page">
    <h1>补证台</h1>
    <p class="muted">
      证据包生成后发现漏条：在批次允许的样本集合内追加或替换条目，补证后重算包指纹、作废旧清单通过态，
      补证成功后须复检清单通过才可放行。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <p v-if="notice" class="tag ok">{{ notice }}</p>

    <div class="panel">
      <h3>选择证据包</h3>
      <div class="row">
        <div class="field">
          <label>证据包</label>
          <select v-model="packId">
            <option v-for="p in packs" :key="p.id" :value="p.id">
              {{ p.pack_no }} · v{{ p.version }} · {{ p.item_count }} 条 · {{ packStatusLabel(p.status) }}
              <template v-if="p.amend_state !== 'none'">（{{ amendStateLabel(p.amend_state) }}）</template>
            </option>
          </select>
        </div>
        <button class="ghost" :disabled="!packId" @click="runVerify">复检互证</button>
      </div>
      <p v-if="selectedPack">
        状态 <span class="tag">{{ packStatusLabel(selectedPack.status) }}</span>
        · 版本 <strong>v{{ selectedPack.version }}</strong>
        · 条目 <strong>{{ selectedPack.item_count }}</strong>
        · 补证态
        <span class="tag" :class="AMEND_STATE_TAG[selectedPack.amend_state]">
          {{ amendStateLabel(selectedPack.amend_state) }}
        </span>
      </p>
      <p v-if="selectedPack && selectedPack.status === 'released'" class="tag danger">
        已放行包禁止补证
      </p>
    </div>

    <div class="panel" v-if="pool">
      <h3>发起补证</h3>
      <div class="row">
        <div class="field">
          <label>提交策略（二选一）</label>
          <select v-model="draftStrategy">
            <option value="incremental">增量补证——保留原条目，追加新条目，版本号加一，旧指纹进历史</option>
            <option value="resign">整包重签——以补证后的完整条目集生成新指纹，旧包版本作废仅审计可查</option>
          </select>
        </div>
        <div class="field">
          <label>失败命运（二选一）</label>
          <select v-model="draftFailure">
            <option value="rollback">失败整笔回滚——包指纹、条目与清单恢复为补证前</option>
            <option value="failed_state">失败进入补证失败态——须显式撤销或再次补证成功后才能继续</option>
          </select>
        </div>
      </div>

      <div class="row">
        <div class="field" style="flex:1;min-width:16rem">
          <label>追加条目（仅可选批次允许集合内、包外条目）</label>
          <select v-model="selectedAdd" multiple style="width:100%;min-height:7rem">
            <option v-for="x in outOfPack" :key="x.business_key" :value="x.business_key">
              {{ x.business_key }} · {{ x.title }} · {{ x.amount }}
            </option>
          </select>
        </div>
        <div class="field">
          <label>替换：包内条目</label>
          <select v-model="replaceFrom" style="min-width:12rem">
            <option value="">（不替换）</option>
            <option v-for="x in inPack" :key="x.business_key" :value="x.business_key">
              {{ x.business_key }} · {{ x.title }}
            </option>
          </select>
        </div>
        <div class="field">
          <label>替换为（批次允许集合内）</label>
          <select v-model="replaceTo" style="min-width:12rem">
            <option value="">（不替换）</option>
            <option v-for="x in outOfPack" :key="x.business_key" :value="x.business_key">
              {{ x.business_key }} · {{ x.title }}
            </option>
          </select>
        </div>
        <button
          :disabled="busy || !amendable || currentAmendState === 'in_progress'"
          @click="createAndSubmit"
        >
          发起并提交补证
        </button>
      </div>
      <p class="muted" style="margin:0">
        <template v-if="draftStrategy === 'incremental'">
          增量补证：旧指纹标记为「{{ HISTORY_STATE.superseded }}」。
        </template>
        <template v-else>
          整包重签：旧包版本标记为「{{ HISTORY_STATE.voided }}」。
        </template>
        <template v-if="draftFailure === 'rollback'">
          失败则整笔回滚，恢复补证前状态。
        </template>
        <template v-else>
          失败则进入补证失败态，须显式撤销或再次补证成功后才能继续。
        </template>
      </p>
    </div>

    <div class="panel" v-if="verify">
      <h3>补证前后互证</h3>
      <p>
        放行闸：
        <span class="tag" :class="verify.release_allowed ? 'ok' : 'danger'">
          {{ verify.release_allowed ? '可放行' : '不可放行' }}
        </span>
        <span v-if="verify.release_blockers.length" class="muted">
          — {{ verify.release_blockers.join('；') }}
        </span>
      </p>
      <p class="muted">
        指纹一致 <strong>{{ verify.checks.fingerprint_matches_items ? '是' : '否' }}</strong>
        · 条数互证 <strong>{{ verify.checks.item_count_matches && verify.checks.row_count_matches ? '是' : '否' }}</strong>
        · 当前版本清单全过
        <strong>{{ verify.checks.checklist_current_all_passed ? '是' : '否' }}</strong>
        · 无残留旧通过态 <strong>{{ verify.checks.no_stale_pass ? '是' : '否' }}</strong>
      </p>
      <table>
        <thead>
          <tr><th>校验互证项</th><th>结果</th></tr>
        </thead>
        <tbody>
          <tr v-for="(v, k) in verify.checks" :key="k">
            <td class="mono">{{ k }}</td>
            <td>
              <span class="tag" :class="v ? 'ok' : 'danger'">{{ v ? '通过' : '不通过' }}</span>
            </td>
          </tr>
        </tbody>
      </table>
      <table style="margin-top:0.75rem">
        <thead>
          <tr><th>版本</th><th>指纹</th><th>条目</th><th>状态</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="h in verify.fingerprint_history" :key="h.id">
            <td>v{{ h.version }}</td>
            <td class="mono" style="max-width:12rem;overflow:hidden;text-overflow:ellipsis">
              {{ h.fingerprint }}
            </td>
            <td>{{ h.item_count }}</td>
            <td>
              <span class="tag" :class="h.state === 'active' ? 'ok' : 'warn'">
                {{ HISTORY_STATE[h.state] || h.state }}
              </span>
            </td>
            <td class="mono">{{ h.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3>补证单留痕</h3>
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>策略</th>
            <th>失败命运</th>
            <th>状态</th>
            <th>条数</th>
            <th>版本</th>
            <th>清单复检</th>
            <th>失败原因</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in amendments" :key="a.id">
            <td>{{ a.id }}</td>
            <td>{{ STRATEGY[a.strategy] || a.strategy }}</td>
            <td>{{ FAILURE_POLICY[a.failure_policy] || a.failure_policy }}</td>
            <td>
              <span
                class="tag"
                :class="a.state === 'succeeded' ? 'ok' : a.state === 'failed' ? 'danger' : a.state === 'cancelled' ? '' : 'warn'"
              >
                {{ AMENDMENT_STATE[a.state] || a.state }}
              </span>
            </td>
            <td>{{ a.before_count ?? '—' }} → {{ a.after_count ?? '—' }}</td>
            <td>{{ a.before_version ? 'v' + a.before_version : '—' }} → {{ a.after_version ? 'v' + a.after_version : '—' }}</td>
            <td>
              <span
                v-if="a.checklist_rerun_passed !== null && a.checklist_rerun_passed !== undefined"
                class="tag"
                :class="a.checklist_rerun_passed ? 'ok' : 'danger'"
              >
                {{ a.checklist_rerun_passed ? '通过' : '未过' }}
              </span>
              <span v-else class="muted">—</span>
            </td>
            <td>{{ a.failure_reason || '—' }}</td>
            <td>
              <button
                v-if="a.state === 'draft' || a.state === 'failed'"
                class="ghost"
                :disabled="busy"
                @click="cancelAmendment(a)"
              >
                撤销
              </button>
            </td>
          </tr>
          <tr v-if="!amendments.length">
            <td colspan="9" class="muted">暂无补证单</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
