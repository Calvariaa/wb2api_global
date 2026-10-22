/* One-off campaign action; daily benefit discovery lives in scheduled tasks. */
window.WBBenefits = (() => {
  async function claimAll() {
    return accountBatchAction(async () => {
      if (!await uiConfirm({kind:'info', danger:false, title:'一键领取 DeepSeek 补偿',
        message:'对全部启用账号领取 500 积分补偿？已领取账号会跳过。请于 10/22 前领取。', okText:'确认一键领取'})) return;
      await runJob({title:'DeepSeek 补偿权益批量领取', iconHtml:'', submitUrl:'/api/benefits/compensation/batch', body:{confirm:true},
        label:item => item.account || item.account_id, detail:item => item.reason || '',
        amount:item => item.credit > 0 ? `<span class="text-emerald-600">+${Number(item.credit)}</span>` : '',
        icon:item => item.ok ? '<i class="fa-solid fa-circle-check text-emerald-500"></i>' : '<i class="fa-solid fa-clock text-amber-600"></i>',
        doneText:job => job.result?.summary || '批量领取已结束', summaryHtml:() => '',
        doneToast:job => job.result?.summary || '批量领取已结束',
        onFinish:() => { loadAccounts(); if (_creditStats) loadCreditStats(); }
      });
    });
  }
  return {claimAll};
})();
