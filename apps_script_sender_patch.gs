// Reviewed integration replacements for the inspected sender.
// Replace the ORIGINAL getCampaignSettings/processNextProspect functions.
// Also replace buildEmailHtml/buildPlainTextEmail with these unchanged copies.
// Do not leave duplicate definitions. Public entry points are in shared_outreach_hooks.gs.

function getCampaignSettingsLegacy_() {

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const campaignSheet = ss.getSheetByName('Campaign');

  if (!campaignSheet) {
    throw new Error('Campaign sheet was not found.');
  }

  const lastRow = Math.max(campaignSheet.getLastRow(), 1);

  const data = campaignSheet
    .getRange(1, 1, lastRow, 2)
    .getValues();

  const campaign = {};

  data.forEach(row => {
    if (row[0]) {
      campaign[String(row[0]).trim()] = row[1];
    }
  });

  return {
    workshopName: String(campaign['Workshop Name'] || '').trim(),

    aiContext: String(
      campaign['AI Context / Instructions'] || ''
    ).trim(),

    targetAudience: String(
      campaign['Target Audience'] || 'All prospects'
    ).trim(),

    workshopDate: formatWorkshopDate_(
  campaign['Workshop Date']
),

    workshopTime: String(
      campaign['Workshop Time'] || ''
    ).trim(),

    workshopDuration: String(
      campaign['Workshop Duration'] || ''
    ).trim(),

    workshopLocation: String(
      campaign['Workshop Location'] || ''
    ).trim(),

    workshopPrice: String(
      campaign['Workshop Price'] || ''
    ).trim(),

    senderName: String(
      campaign['Sender Name'] || ''
    ).trim(),

    topPosterUrl: String(
      campaign['Top Poster URL'] || ''
    ).trim(),

    bottomPosterUrl: String(
      campaign['Bottom Poster URL'] || ''
    ).trim(),

    topPosterFileId: String(campaign['Top Poster File ID'] || '').trim(),
    bottomPosterFileId: String(campaign['Bottom Poster File ID'] || '').trim(),
    brochureFileIds: String(campaign['Brochure File IDs'] || '').trim(),
    topPosterLinkUrl: String(campaign['Top Poster Click URL'] || '').trim(),
    bottomPosterLinkUrl: String(campaign['Bottom Poster Click URL'] || '').trim(),

    knowMoreUrl: String(
      campaign['Know More URL'] || ''
    ).trim(),

    registrationUrl: String(
      campaign['Registration URL'] || ''
    ).trim(),

    chatbotUrl: String(
      campaign['Chatbot URL'] || ''
    ).trim(),

    scheduleCallUrl: String(
      campaign['Schedule a Call URL'] || ''
    ).trim()
};
}

// ============================================================
// 3. CONVERT GOOGLE DRIVE POSTER URL
// ============================================================


function processNextProspectLegacy_() {

  const sheet =
    getProspectSheet();

  const data =
    sheet
      .getDataRange()
      .getValues();

  const headers =
    data[0].map(header =>
      String(header)
        .trim()
        .toLowerCase()
    );

  const nameCol =
    headers.indexOf('name');

  const companyCol =
    headers.indexOf('company');

  const jobTitleCol =
    headers.indexOf('job title');

  const emailCol =
    headers.indexOf('email');

  const statusCol =
    headers.indexOf('status');

  const subjectCol =
    headers.indexOf('subject');

  const messageCol =
    headers.indexOf('message');

  let errorCol =
    headers.indexOf('error');


  // ----------------------------------------------------------
  // CREATE ERROR COLUMN IF MISSING
  // ----------------------------------------------------------

  if (errorCol === -1) {

    errorCol =
      headers.length;

    sheet
      .getRange(
        1,
        errorCol + 1
      )
      .setValue('Error');

  }


  if (
    nameCol === -1 ||
    companyCol === -1 ||
    jobTitleCol === -1 ||
    emailCol === -1 ||
    statusCol === -1 ||
    subjectCol === -1 ||
    messageCol === -1
  ) {

    throw new Error(
      'Required columns are missing.'
    );

  }


  // ----------------------------------------------------------
  // FIND FIRST PENDING PROSPECT
  // ----------------------------------------------------------

  let rowIndex = -1;

  for (
    let i = 1;
    i < data.length;
    i++
  ) {

    const email =
      String(
        data[i][emailCol] || ''
      )
        .trim()
        .toLowerCase();

    const status =
      String(
        data[i][statusCol] || ''
      )
        .trim();

    if (
      email &&
      status === 'Pending'
    ) {

      rowIndex = i;
      break;

    }

  }


  // ----------------------------------------------------------
  // NO PENDING PROSPECTS
  // ----------------------------------------------------------

  if (rowIndex === -1) {

    return {

      success: true,

      completed: true,

      message:
        'No pending prospects remaining.'

    };

  }


  const rowNumber =
    rowIndex + 1;


  const name =
    String(
      data[rowIndex][nameCol] || ''
    ).trim();


  const company =
    String(
      data[rowIndex][companyCol] || ''
    ).trim();


  const jobTitle =
    String(
      data[rowIndex][jobTitleCol] || ''
    ).trim();


  const email =
    String(
      data[rowIndex][emailCol] || ''
    )
      .trim()
      .toLowerCase();


  try {


    // ========================================================
    // DUPLICATE EMAIL CHECK
    // ========================================================

    for (
      let i = 1;
      i < data.length;
      i++
    ) {

      if (i === rowIndex) {
        continue;
      }

      const existingEmail =
        String(
          data[i][emailCol] || ''
        )
          .trim()
          .toLowerCase();

      const existingStatus =
        String(
          data[i][statusCol] || ''
        )
          .trim();

      if (
        existingEmail === email &&
        existingStatus === 'Sent'
      ) {

        sheet
          .getRange(
            rowNumber,
            statusCol + 1
          )
          .setValue(
            'Duplicate - Not Sent'
          );

        sheet
          .getRange(
            rowNumber,
            errorCol + 1
          )
          .setValue(
            'This email address was already sent a campaign email.'
          );

        return {

          success: true,

          completed: false,

          email: email,

          status:
            'Duplicate - Not Sent'

        };

      }

    }


    // ========================================================
    // GET CAMPAIGN SETTINGS
    // ========================================================

    const campaign =
      getCampaignSettings();


    if (
      !campaign.workshopName ||
      !campaign.aiContext
    ) {

      throw new Error(
        'Workshop Name and AI Context / Instructions are required.'
      );

    }


    if (!campaign.senderName) {

      throw new Error(
        'Sender Name is required.'
      );

    }


    const sharedDelivery = SharedOutreach.prepare(sheet, rowNumber, headers, data[rowIndex], campaign);

    // ========================================================
    // MARK GENERATING
    // ========================================================

    sheet
      .getRange(
        rowNumber,
        statusCol + 1
      )
      .setValue(
        'Generating'
      );

    sheet
      .getRange(
        rowNumber,
        errorCol + 1
      )
      .setValue('');

    SpreadsheetApp.flush();


    // ========================================================
    // GENERATE PERSONALIZED BODY
    // ========================================================

    const personalizedMessage =
      generateMessageForProspect(
        name,
        company,
        jobTitle
      );


    // ========================================================
    // SUBJECT
    // ========================================================

    const emailSubject =
      campaign.workshopName;


    // ========================================================
    // SAVE SUBJECT AND MESSAGE
    // ========================================================

    sheet
      .getRange(
        rowNumber,
        subjectCol + 1
      )
      .setValue(
        emailSubject
      );

    sheet
      .getRange(
        rowNumber,
        messageCol + 1
      )
      .setValue(
        personalizedMessage
      );

    sheet
      .getRange(
        rowNumber,
        statusCol + 1
      )
      .setValue(
        'Message Generated'
      );

    SpreadsheetApp.flush();


    // ========================================================
    // BUILD HTML EMAIL
    // ========================================================

    const htmlBody =
      buildEmailHtml(
        name,
        personalizedMessage,
        campaign
      );


    // ========================================================
    // BUILD PLAIN TEXT EMAIL
    // ========================================================

    const plainTextBody =
      buildPlainTextEmail(
        name,
        personalizedMessage,
        campaign
      );


    // ========================================================
    // SEND EMAIL
    // ========================================================

    const sendOptions = {
      to: email,
      subject: emailSubject,
      body: plainTextBody,
      htmlBody: htmlBody,
      name: campaign.senderName
    };

    // Uploaded posters are embedded inline; brochures are attachments.
    const inlineImages = {};
    if (campaign.topPosterFileId) {
      inlineImages.topPoster = DriveApp.getFileById(campaign.topPosterFileId).getBlob();
    }
    if (campaign.bottomPosterFileId) {
      inlineImages.bottomPoster = DriveApp.getFileById(campaign.bottomPosterFileId).getBlob();
    }
    if (Object.keys(inlineImages).length) sendOptions.inlineImages = inlineImages;

    const brochureIds = String(campaign.brochureFileIds || '').split(',').map(function(id) { return id.trim(); }).filter(Boolean);
    if (brochureIds.length) {
      sendOptions.attachments = brochureIds.map(function(id) {
        return DriveApp.getFileById(id).getBlob();
      });
    }

    SharedOutreach.beforeSend(sharedDelivery);
    try {
      MailApp.sendEmail(sendOptions);
    } catch (providerError) {
      SharedOutreach.uncertain(sharedDelivery);
      throw new Error('Provider acceptance is uncertain. Check the sending account before retrying.');
    }
    SharedOutreach.accepted(sharedDelivery);


    // ========================================================
    // MARK SENT
    // ========================================================

    sheet
      .getRange(
        rowNumber,
        statusCol + 1
      )
      .setValue(
        'Sent'
      );

    sheet
      .getRange(
        rowNumber,
        errorCol + 1
      )
      .setValue('');

    SpreadsheetApp.flush();


    Logger.log(
      'Successfully sent to: ' +
      email
    );


    return {

      success: true,

      completed: false,

      email: email,

      status:
        'Sent'

    };


  } catch (error) {

    const errorMessage =
      error && error.message
        ? error.message
        : String(error);


    sheet
      .getRange(
        rowNumber,
        statusCol + 1
      )
      .setValue(
        'Error'
      );


    sheet
      .getRange(
        rowNumber,
        errorCol + 1
      )
      .setValue(
        errorMessage
      );


    SpreadsheetApp.flush();


    Logger.log(
      'Error processing ' +
      email +
      ': ' +
      errorMessage
    );


    return {

      success: false,

      completed: false,

      email: email,

      status:
        'Error',

      error:
        errorMessage

    };

  }

}


// ============================================================
// 5. PROCESS SMALL BATCH
// ============================================================


function buildEmailHtml(
  name,
  message,
  campaign
) {
  const fontStack = "'Segoe UI', Tahoma, Arial, sans-serif";
  const safeWorkshopName = escapeHtml(campaign.workshopName || 'Workshop Invitation');
  const safeMessage = formatEmailMessageHtml_(message, campaign, name);
  const safeSender = escapeHtml(campaign.senderName || '');

  const safeDate = escapeHtml(campaign.workshopDate || '');
  const safeTime = escapeHtml(campaign.workshopTime || '');
  const safeDuration = escapeHtml(campaign.workshopDuration || '');
  const safeLocation = escapeHtml(campaign.workshopLocation || '');
  const safePrice = escapeHtml(campaign.workshopPrice || '');

  const topPosterDirectUrl = convertGoogleDriveImageUrl(campaign.topPosterUrl);
  const bottomPosterDirectUrl = convertGoogleDriveImageUrl(campaign.bottomPosterUrl);

  const topPoster = campaign.topPosterFileId
    ? 'cid:topPoster'
    : (topPosterDirectUrl ? escapeHtml(topPosterDirectUrl) : '');

  const bottomPoster = campaign.bottomPosterFileId
    ? 'cid:bottomPoster'
    : (bottomPosterDirectUrl ? escapeHtml(bottomPosterDirectUrl) : '');

  const safeKnowMore = campaign.knowMoreUrl ? escapeHtml(campaign.knowMoreUrl) : '';
  const safeRegistration = campaign.registrationUrl ? escapeHtml(campaign.registrationUrl) : '';
  const safeChat = campaign.chatbotUrl ? escapeHtml(campaign.chatbotUrl) : '';

  // Only display campaign details that have actually been supplied.
  const detailRows = [];
  [
    ['Date', campaign.workshopDate, safeDate],
    ['Time', campaign.workshopTime, safeTime],
    ['Duration', campaign.workshopDuration, safeDuration],
    ['Location', campaign.workshopLocation, safeLocation],
    ['Price', campaign.workshopPrice, safePrice]
  ].forEach(function(item) {
    if (item[1] && String(item[1]).trim()) {
      detailRows.push(
        '<tr>' +
          '<td style="padding:8px 12px 8px 0;width:34%;font-size:14px;font-weight:600;color:#536273;">' +
            escapeHtml(item[0]) +
          '</td>' +
          '<td style="padding:8px 0;font-size:14px;color:#1F2937;">' + item[2] + '</td>' +
        '</tr>'
      );
    }
  });

  const detailsSection = detailRows.length
    ? '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" ' +
        'style="width:100%;margin:24px 0 8px;background:#F6F9FC;border:1px solid #E5EAF0;border-radius:12px;">' +
        '<tr><td style="padding:14px 18px;">' +
          '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">' +
            detailRows.join('') +
          '</table>' +
        '</td></tr></table>'
    : '';

  function posterSection(url, alt, clickUrl) {
    if (!url) return '';
    const image =
      '<img src="' + url + '" alt="' + escapeHtml(alt) + '" width="600" ' +
      'style="display:block;width:100%;max-width:600px;height:auto;border:0;border-radius:18px;">';
    const content = clickUrl
      ? '<a href="' + escapeHtml(clickUrl) + '" target="_blank" style="display:block;text-decoration:none;">' + image + '</a>'
      : image;
    return '<tr><td style="padding:0 0 18px 0;">' + content + '</td></tr>';
  }

  function button(url, label, background, color, borderColor) {
    if (!url) return '';
    return '<td align="center" style="padding:5px 4px;">' +
      '<a href="' + url + '" target="_blank" ' +
      'style="display:inline-block;padding:13px 19px;background:' + background + ';color:' + color + ';' +
      'font-family:' + fontStack + ';font-size:14px;line-height:18px;font-weight:600;text-decoration:none;' +
      'border:1px solid ' + borderColor + ';border-radius:9px;">' + label + '</a>' +
      '</td>';
  }

  const ctaButtons = [
    button(safeRegistration, 'Register Now', '#2455C5', '#FFFFFF', '#2455C5'),
    button(safeKnowMore, 'Know More', '#FFFFFF', '#2455C5', '#D7E2F2'),
    button(safeChat, 'Chat With Us', '#EAF7F3', '#087F70', '#CDEBE2')
  ].filter(Boolean).join('');

  const ctaSection = ctaButtons
    ? '<table role="presentation" align="center" cellpadding="0" cellspacing="0" border="0" ' +
        'style="margin:24px auto 8px;"><tr>' + ctaButtons + '</tr></table>'
    : '';

  return '<!DOCTYPE html>' +
    '<html><head><meta charset="UTF-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">' +
    '<title>' + safeWorkshopName + '</title></head>' +
    '<body style="margin:0;padding:0;background-color:#E8EDF4;font-family:' + fontStack + ';">' +

    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" ' +
      'style="width:100%;background-color:#E8EDF4;">' +
      '<tr><td align="center" style="padding:28px 12px;">' +

        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" ' +
          'style="width:100%;max-width:680px;">' +

          // Google-inspired top visual: soft pastel hero, not Google branding.
          '<tr><td style="padding:30px 34px 28px;background-color:#F3FFF0;border-radius:28px 28px 0 0;">' +
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">' +
              '<tr><td style="font-family:' + fontStack + ';font-size:32px;line-height:39px;' +
                'font-weight:700;letter-spacing:-0.7px;color:#14243A;padding:0 0 10px;">' +
                safeWorkshopName +
              '</td></tr>' +
              '<tr><td style="font-family:' + fontStack + ';font-size:15px;line-height:24px;color:#405166;">' +
                'A practical learning experience for professionals.' +
              '</td></tr>' +
              '<tr><td style="padding-top:18px;">' +
                '<div style="width:54px;height:4px;background:#0F8B78;border-radius:4px;font-size:0;line-height:0;">&nbsp;</div>' +
              '</td></tr>' +
            '</table>' +
          '</td></tr>' +

          // Optional top poster appears directly below the hero.
          (topPoster ? '<tr><td style="padding:0 0 18px;background:#FFFFFF;">' +
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">' +
              '<tr><td>' +
                (campaign.topPosterLinkUrl
                  ? '<a href="' + escapeHtml(campaign.topPosterLinkUrl) + '" target="_blank" style="display:block;text-decoration:none;">'
                  : '') +
                '<img src="' + topPoster + '" alt="' + safeWorkshopName + '" width="680" ' +
                  'style="display:block;width:100%;max-width:680px;height:auto;border:0;">' +
                (campaign.topPosterLinkUrl ? '</a>' : '') +
              '</td></tr></table></td></tr>' : '') +

          // Main personal message: paragraphs, no bullet list.
          '<tr><td style="padding:28px 30px 34px;background:#FFFFFF;">' +
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" ' +
              'style="width:100%;background:#EAF3FF;border-radius:22px;">' +
              '<tr><td style="padding:27px 28px 29px;">' +
                '<div style="font-family:' + fontStack + ';font-size:16px;line-height:1.85;color:#172B4D;">' +
                  safeMessage +
                '</div>' +
              '</td></tr>' +
            '</table>' +
            detailsSection +
            '<div style="margin-top:20px;font-family:' + fontStack + ';font-size:14px;line-height:1.75;color:#536273;">' +
              'If this sounds relevant to you, I would be happy to share further details or answer any questions.' +
            '</div>' +
            ctaSection +
            (bottomPoster ? '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" ' +
              'style="margin-top:24px;"><tr><td>' +
                (campaign.bottomPosterLinkUrl
                  ? '<a href="' + escapeHtml(campaign.bottomPosterLinkUrl) + '" target="_blank" style="display:block;text-decoration:none;">'
                  : '') +
                '<img src="' + bottomPoster + '" alt="' + safeWorkshopName + '" width="600" ' +
                  'style="display:block;width:100%;max-width:600px;height:auto;border:0;border-radius:16px;">' +
                (campaign.bottomPosterLinkUrl ? '</a>' : '') +
              '</td></tr></table>' : '') +
          '</td></tr>' +

          '<tr><td style="padding:22px 38px 26px;background:#F8FAFC;border-top:1px solid #E5EAF0;border-radius:0 0 18px 18px;">' +
            '<div style="font-family:' + fontStack + ';font-size:13px;line-height:20px;color:#667085;">Best regards,</div>' +
            '<div style="margin-top:4px;font-family:' + fontStack + ';font-size:15px;line-height:22px;font-weight:700;color:#2455C5;">' +
              safeSender +
            '</div>' +
          '</td></tr>' +

        '</table>' +
      '</td></tr>' +
    '</table>' +
    '</body></html>';
}


// ============================================================
// 9. PLAIN TEXT EMAIL
// ============================================================


function buildPlainTextEmail(
  name,
  message,
  campaign
) {

  let text = message;

  const hasCampaignDetails = campaign.workshopDate || campaign.workshopTime || campaign.workshopDuration || campaign.workshopLocation || campaign.workshopPrice;
  if (hasCampaignDetails) text += '\n\n----------------------------------------\nCAMPAIGN DETAILS\n----------------------------------------\n';

  if (campaign.workshopDate) {
    text +=
      'Date: ' +
      campaign.workshopDate +
      '\n';
  }

  if (campaign.workshopTime) {
    text +=
      'Time: ' +
      campaign.workshopTime +
      '\n';
  }

  if (campaign.workshopDuration) {
    text +=
      'Duration: ' +
      campaign.workshopDuration +
      '\n';
  }

  if (campaign.workshopLocation) {
    text +=
      'Location: ' +
      campaign.workshopLocation +
      '\n';
  }

  if (campaign.workshopPrice) {
    text +=
      'Price: ' +
      campaign.workshopPrice +
      '\n';
  }

  if (campaign.knowMoreUrl) {
    text +=
      '\nKnow More:\n' +
      campaign.knowMoreUrl +
      '\n';
  }

  if (campaign.registrationUrl) {
    text +=
      '\nRegister Now:\n' +
      campaign.registrationUrl +
      '\n';
  }

  if (campaign.chatbotUrl) {
    text +=
      '\nChat With Us:\n' +
      campaign.chatbotUrl +
      '\n';
  }

  text +=
    '\n\nBest regards,\n' +
    campaign.senderName;

  return text;
}

// ============================================================
// 10. HTML ESCAPE
// ============================================================

