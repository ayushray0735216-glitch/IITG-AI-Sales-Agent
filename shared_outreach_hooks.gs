/**
 * Integration hooks for the inspected Personalised message sender.
 * Install alongside shared_crm_adapter.gs and apps_script_sender_patch.gs.
 * No trigger or provider call runs on import. Disabled until explicitly configured.
 */
var SharedOutreach = (function () {
  var activeSnapshot = null;
  function prop(name) {
    return String(PropertiesService.getScriptProperties().getProperty(name) || '').trim();
  }
  function enabled() { return prop('SHARED_CRM_ENABLED') === 'true'; }
  function snapshot() { return activeSnapshot || SharedCRM.currentSnapshot(); }
  function table(sheet) {
    var headings = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    var indexes = {};
    headings.forEach(function (h, i) {
      var key = String(h).trim().toLowerCase();
      if (key && indexes[key] !== undefined) throw new Error('Duplicate prospect headings need review.');
      if (key) indexes[key] = i + 1;
    });
    return {
      get: function (row, name) {
        var col = indexes[name.toLowerCase()];
        return col ? String(sheet.getRange(row, col).getValue() || '').trim() : '';
      },
      set: function (row, name, value) {
        var key = name.toLowerCase(), col = indexes[key];
        if (!col) {
          col = sheet.getLastColumn() + 1;
          sheet.getRange(1, col).setValue(name);
          indexes[key] = col;
        }
        sheet.getRange(row, col).setValue(value);
      }
    };
  }
  function chosenOffering(current) {
    var offeringId = prop('SHARED_CRM_OFFERING_ID');
    var offering = (current.offerings || []).find(function (o) { return o['Offering ID'] === offeringId; });
    if (!offeringId || !offering) throw new Error('Select a currently published shared offering by its Offering ID.');
    return offering;
  }
  function checkPermission(contact, current, review) {
    var policy = current.outreach_policy || {}, prefs = contact.preferences || {};
    if (contact.qualification !== 'qualified') throw new Error('This prospect has not been qualified for outreach.');
    if (prefs.consent !== true || (prefs.channels || []).indexOf('email') < 0) {
      throw new Error('Recorded email contact permission is required. A discovered address is not consent.');
    }
    if (policy.enabled !== true || !policy.channels || policy.channels.email !== true) {
      throw new Error('Email outreach is paused in the shared admin policy.');
    }
    // The initial connector remains staff-reviewed even when Autopilot is on.
    // Approval is not inferred from Pending/Sent, qualification, or an email click.
    if (review !== 'Approved') throw new Error('Set Shared Review to Approved after reviewing this recipient and campaign.');
    var hour = Number(Utilities.formatDate(new Date(), 'Asia/Kolkata', 'H'));
    if (!(hour >= policy.quiet_start_hour && hour < policy.quiet_end_hour)) {
      throw new Error('The shared contact window is closed.');
    }
  }
  function record(context, status) {
    return SharedCRM.recordOutcome(context.eventId + ':' + status, {
      contact_id: context.contactId, campaign_id: context.campaignId,
      channel: 'email', status: status
    });
  }
  function safeResult(context, status) {
    if (!context) return;
    // Never let a secondary sheet/API failure re-enter the provider send path.
    // A previously persisted Sending marker blocks automatic retries after a crash.
    try { context.cells.set(context.row, 'Shared Send State', status === 'sent' ? 'Sent' : 'Uncertain'); }
    catch (_) { Logger.log('Shared send state needs manual reconciliation.'); }
    try {
      record(context, status);
      context.cells.set(context.row, 'Shared Sync State', 'Recorded');
    } catch (_) {
      try { context.cells.set(context.row, 'Shared Sync State', 'Pending ' + status); }
      catch (ignored) { /* Sending marker still prevents a blind retry. */ }
      Logger.log('Shared outcome needs reconciliation; do not resend.');
    }
  }
  return {
    enabled: enabled,
    run: function (sender) {
      if (!enabled()) return sender();
      var lock = LockService.getScriptLock();
      if (!lock.tryLock(1000)) throw new Error('Another outreach task is already running.');
      try {
        activeSnapshot = SharedCRM.currentSnapshot();
        chosenOffering(activeSnapshot);
        return sender();
      } finally { activeSnapshot = null; lock.releaseLock(); }
    },
    campaign: function (legacy) {
      if (!enabled()) return legacy;
      var current = snapshot(), offering = chosenOffering(current);
      // Sender identity/posters remain campaign settings. Organization facts are
      // replaced from the same authoritative catalog used by chat and voice.
      return Object.assign({}, legacy, {
        workshopName: String(offering['Offering Name'] || ''),
        aiContext: JSON.stringify(offering), targetAudience: String(offering['Target Audience'] || ''),
        workshopDate: [offering['Start Date'], offering['End Date']].filter(Boolean).join(' to '),
        workshopTime: [offering['Start Time'], offering['End Time']].filter(Boolean).join(' to '),
        workshopDuration: String(offering.Duration || ''), workshopLocation: String(offering.Location || offering.Mode || ''),
        workshopPrice: [offering.Price, offering.Currency].filter(function (v) { return v !== undefined && v !== null && v !== ''; }).join(' '),
        registrationUrl: String(offering['Registration Link'] || ''),
        knowMoreUrl: String(offering['Website URL'] || ''), chatbotUrl: ''
      });
    },
    prepare: function (sheet, row, headings, values, campaign) {
      if (!enabled()) return null;
      var cells = table(sheet), campaignId = prop('SHARED_CRM_CAMPAIGN_ID');
      if (!campaignId) throw new Error('Set a stable shared campaign reference in Script Properties.');
      if (cells.get(row, 'Shared Campaign ID') && cells.get(row, 'Shared Campaign ID') !== campaignId) {
        throw new Error('Use a separate campaign sheet for a new campaign; do not overwrite an earlier delivery record.');
      }
      if (/^(Sent|Sending|Uncertain)$/.test(cells.get(row, 'Shared Send State'))) {
        throw new Error('This recipient already has a sent or uncertain delivery. Reconcile it before any retry.');
      }
      var qualification = cells.get(row, 'Qualification').toLowerCase();
      var fields = SharedCRM.prospectFromSheetRow(headings, values, qualification);
      var prospectId = cells.get(row, 'Prospect ID');
      if (!prospectId) { prospectId = 'PRO-' + Utilities.getUuid(); cells.set(row, 'Prospect ID', prospectId); }
      SpreadsheetApp.flush();
      var contactId = cells.get(row, 'Shared Contact ID');
      // Do not overwrite newer admin changes with an older spreadsheet copy.
      // A first-time import colliding with an existing contact requires review.
      var contact = contactId ? SharedCRM.getContact(contactId) : SharedCRM.upsertProspect(prospectId, fields, 0);
      if (contact.email.toLowerCase() !== fields.email.toLowerCase()) throw new Error('Review the current shared email address before sending.');
      checkPermission(contact, snapshot(), cells.get(row, 'Shared Review'));
      var eventId = cells.get(row, 'Shared Event ID') || 'MAIL-' + Utilities.getUuid();
      var chatUrl = cells.get(row, 'Shared Chat URL');
      var expires = Number(cells.get(row, 'Shared Link Expires'));
      if (!chatUrl || expires <= Date.now() / 1000) {
        var link = SharedCRM.createChatLink(contact.contact_id, campaignId);
        chatUrl = link.chat_url; expires = link.expires_at;
      }
      var valuesToSave = {'Shared Contact ID': contact.contact_id, 'Shared Campaign ID': campaignId,
        'Shared Event ID': eventId, 'Shared Chat URL': chatUrl, 'Shared Link Expires': expires,
        'Shared Send State': 'Prepared'};
      Object.keys(valuesToSave).forEach(function (key) { cells.set(row, key, valuesToSave[key]); });
      SpreadsheetApp.flush();
      campaign.chatbotUrl = chatUrl;
      return {cells: cells, row: row, contactId: contact.contact_id, campaignId: campaignId,
        eventId: eventId, email: fields.email.toLowerCase(), version: snapshot().version};
    },
    beforeSend: function (context) {
      if (!context) return;
      var current = SharedCRM.currentSnapshot();
      if (current.version !== context.version) throw new Error('Shared information changed during drafting. Review a fresh draft before sending.');
      var contact = SharedCRM.getContact(context.contactId);
      if (contact.email.toLowerCase() !== context.email) throw new Error('The recipient email changed. Review the updated contact.');
      checkPermission(contact, current, context.cells.get(context.row, 'Shared Review'));
      context.cells.set(context.row, 'Shared Send State', 'Sending');
      SpreadsheetApp.flush();
      // Persist locally BEFORE reserving. A lost response or crash blocks a blind
      // retry. Only one connected sender can reserve this recipient/campaign.
      SharedCRM.reserveEmail(context.contactId, context.campaignId, context.eventId, current.version);
    },
    accepted: function (context) { safeResult(context, 'sent'); },
    uncertain: function (context) { safeResult(context, 'uncertain'); },
    reconcileOutcome: function (sheet, row) {
      // Records only a known result. Never sends an email or clears Sending.
      var cells = table(sheet), state = cells.get(row, 'Shared Send State');
      if (state !== 'Sent' && state !== 'Uncertain') throw new Error('Verify the provider result before reconciling this row.');
      record({eventId: cells.get(row, 'Shared Event ID'), contactId: cells.get(row, 'Shared Contact ID'),
        campaignId: cells.get(row, 'Shared Campaign ID')}, state === 'Sent' ? 'sent' : 'uncertain');
      cells.set(row, 'Shared Sync State', 'Recorded');
    }
  };
})();

function processNextProspect() { return SharedOutreach.run(processNextProspectLegacy_); }
function getCampaignSettings() { return SharedOutreach.campaign(getCampaignSettingsLegacy_()); }
