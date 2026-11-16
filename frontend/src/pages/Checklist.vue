<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, postJSON } from '../api'
import { amendStateLabel, packStatusLabel } from '../labels'

const packs = ref([])
const packId = ref(null)
const result = ref(null)
const history = ref([])
const err = ref('')
const busy = ref(false)

async function refresh() {
  packs.value = await getJSON('/api/packs')
  if (!packId.value && packs.value.length) packId.value = packs.value[0].id
  if (packId.value) {
    history.value = await getJSON(`/api/checklist?pack_id=${packId.value}`)
  }
}

onMounted(async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})

async function run() {
  err.value = ''
  busy.value = true
  try {
    result.value = await postJSON('/api/checklist/run', { pack_id: Number(packId.value) })
    history.value = result.value.results || []
    packs.value = await getJSON('/api/packs')
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function onPackChange() {
  history.value = await getJSON(`/api/checklist?pack_id=${packId.value}`)
  result.value = null
}
</script>
<template>
  <div class="page">
    <h1>校验清单</h1>
    <p class="muted">基础项：条数一致、键唯一、必填非空。结果与包内条目互证；补证后旧版本结果作废，仅审计可查。</p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel">
      <h3>执行校验</h3>
      <div class="row">
        <div class="field">
          <label>证据包</label>
          <select v-model="packId" @change="onPackChange">
            <option v-for="p in packs" :key="p.id" :value="p.id">
              {{ p.pack_no }}（v{{ p.version }} · {{ p.item_count }} 条 · {{ packStatusLabel(p.status) }}
              <template v-if="p.amend_state !== 'none'"> · {{ amendStateLabel(p.amend_state) }}</template>）
            </option>
          </select>
        </div>
        <button :disabled="busy || !packId" @click="run">跑校验清单</button>
      </div>
      <p v-if="result">
        条目数 <strong>{{ result.item_count }}</strong>
        ·
        <span class="tag" :class="result.all_passed ? 'ok' : 'danger'">
          {{ result.all_passed ? '全部通过' : '存在未通过项' }}
        </span>
      </p>
    </div>
    <div class="panel">
      <h3>清单结果</h3>
      <table>
        <thead>
          <tr>
            <th>校验项</th>
            <th>版本</th>
            <th>结果</th>
            <th>说明</th>
            <th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in history" :key="r.id || r.check_code">
            <td>{{ r.check_title }}</td>
            <td>v{{ r.pack_version }}</td>
            <td>
              <span class="tag" :class="r.voided ? 'warn' : r.passed ? 'ok' : 'danger'">
                {{ r.voided ? '已作废（仅审计）' : r.passed ? '通过' : '未通过' }}
              </span>
            </td>
            <td>{{ r.detail }}</td>
            <td class="mono">{{ r.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
