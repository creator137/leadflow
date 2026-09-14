/** LeadFlow actions for a bound Google Spreadsheet. Secrets live only in Script Properties. */
function onOpen() {
  SpreadsheetApp.getUi().createMenu('LeadFlow')
    .addItem('Подготовить КП', 'prepareProposal')
    .addItem('Отправить КП', 'sendProposal')
    .addSeparator().addItem('Настроить подключение', 'configureLeadFlow').addToUi();
}

function configureLeadFlow() {
  const ui = SpreadsheetApp.getUi();
  const base = ui.prompt('Адрес LeadFlow', 'Например: https://leadflow.example.ru', ui.ButtonSet.OK_CANCEL);
  if (base.getSelectedButton() !== ui.Button.OK) return;
  const token = ui.prompt('Ключ подключения', 'Ключ будет сохранён в защищённых свойствах сценария, не в ячейках.', ui.ButtonSet.OK_CANCEL);
  if (token.getSelectedButton() !== ui.Button.OK) return;
  PropertiesService.getScriptProperties().setProperties({LEADFLOW_URL: base.getResponseText().replace(/\/$/, ''), LEADFLOW_TOKEN: token.getResponseText()});
  ui.alert('Подключение сохранено.');
}

function prepareProposal() { runLeadFlowAction_('prepare'); }
function sendProposal() { runLeadFlowAction_('send'); }

function runLeadFlowAction_(action) {
  const sheet = SpreadsheetApp.getActiveSheet();
  const activeRow = sheet.getActiveCell().getRow();
  const values = sheet.getDataRange().getDisplayValues();
  const headerRow = values.findIndex(r => r.indexOf('Наименование клиента') >= 0) + 1;
  if (!headerRow || activeRow <= headerRow) throw new Error('Выберите строку компании.');
  const idColumn = values[headerRow - 1].indexOf('LeadFlow ID');
  if (idColumn < 0) throw new Error('Не найден служебный идентификатор LeadFlow.');
  const companyId = values[activeRow - 1][idColumn];
  if (!companyId) throw new Error('В выбранной строке нет компании LeadFlow.');
  const props = PropertiesService.getScriptProperties();
  const base = props.getProperty('LEADFLOW_URL'), token = props.getProperty('LEADFLOW_TOKEN');
  if (!base || !token) throw new Error('Сначала настройте подключение в меню LeadFlow.');
  SpreadsheetApp.getActive().toast(action === 'prepare' ? 'Готовим персональное КП…' : 'Отправляем КП…', 'LeadFlow', -1);
  const response = UrlFetchApp.fetch(base + '/api/google-sheets/actions/' + action, {method:'post', contentType:'application/json', headers:{'X-LeadFlow-Sheet-Token':token}, payload:JSON.stringify({company_id:companyId}), muteHttpExceptions:true});
  let body = {}; try { body = JSON.parse(response.getContentText()); } catch (_) {}
  if (response.getResponseCode() >= 300) throw new Error(body.detail || 'LeadFlow не смог выполнить действие.');
  SpreadsheetApp.getActive().toast(body.status, 'LeadFlow', 6);
  if (body.preview_url && action === 'prepare') SpreadsheetApp.getUi().alert('КП подготовлено', 'Откройте предпросмотр: ' + body.preview_url, SpreadsheetApp.getUi().ButtonSet.OK);
}
