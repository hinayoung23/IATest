    var IATEST_TRANSLATIONS = clientTranslations();
    var IATEST_LOCALE = IATEST_TRANSLATIONS.locale;
    var CLIENT_I18N = IATEST_TRANSLATIONS.catalog;

    function resetDialog(dialog) {
      var form = dialog ? dialog.querySelector('form') : null;
      if (form) form.reset();
      if (dialog) {
        dialog.querySelectorAll('[data-secret-input]').forEach(function(input) {
          input.type = 'text';
          updateSecretToggle(input);
        });
        dialog.querySelectorAll('[data-source-edit]').forEach(function(form) {
          updateSourceMode(form);
        });
        dialog.querySelectorAll('[data-body-editor]').forEach(function(editor) {
          updateBodyEditor(editor);
        });
      }
    }
    function cancelDialog(dialogId) {
      var dialog = document.getElementById(dialogId);
      resetDialog(dialog);
      if (dialog) dialog.close();
    }
    function updateSourceMode(form) {
      if (!form) return;
      var mode = form.querySelector('[data-source-mode]');
      var value = mode ? mode.value : 'file';
      form.querySelectorAll('[data-source-section]').forEach(function(section) {
        var active = section.dataset.sourceSection === value;
        section.hidden = !active;
        section.style.display = active ? 'grid' : 'none';
      });
    }
    function updateBodyEditor(editor) {
      if (!editor) return;
      var mode = editor.querySelector('[data-body-mode]');
      var value = mode ? mode.value : '';
      editor.querySelectorAll('[data-body-section]').forEach(function(section) {
        var name = section.dataset.bodySection;
        var active = (name === 'schema' && (value === 'json' || value === 'form' || value === 'multipart')) || (name === 'file' && value === 'file');
        section.hidden = !active;
        section.style.display = active ? 'grid' : 'none';
      });
    }
    function taskProject() {
      return new URLSearchParams(window.location.search).get('project') || 'default';
    }
    function taskDismissKey(task) {
      return 'iatest-task-dismissed-' + task.id + '-' + task.status;
    }
    var TASK_STAGES = {
      discover: [
        {key: 'queued', label: tr('stage.queued')},
        {key: 'read', label: tr('stage.read_document')},
        {key: 'parse', label: tr('stage.parse_document')},
        {key: 'llm', label: tr('stage.llm_discovery')},
        {key: 'audit', label: tr('stage.audit')},
        {key: 'save', label: tr('stage.save_results')},
        {key: 'complete', label: tr('stage.complete')}
      ],
      generate: [
        {key: 'queued', label: tr('stage.queued')},
        {key: 'read', label: tr('stage.read_endpoints')},
        {key: 'prepare', label: tr('stage.prepare')},
        {key: 'llm', label: tr('stage.llm_generate')},
        {key: 'audit', label: tr('stage.audit')},
        {key: 'save', label: tr('stage.save_cases')},
        {key: 'complete', label: tr('stage.complete')}
      ],
      run: [
        {key: 'queued', label: tr('stage.queued')},
        {key: 'read', label: tr('stage.read_cases')},
        {key: 'filter', label: tr('stage.filter')},
        {key: 'execute', label: tr('stage.execute')},
        {key: 'audit', label: tr('stage.audit_report')},
        {key: 'report', label: tr('stage.generate_report')},
        {key: 'complete', label: tr('stage.complete')}
      ]
    };
    function renderTaskSteps(task, container) {
      if (!container) return;
      var stages = TASK_STAGES[task.action] || [];
      var currentIndex = stages.findIndex(function(stage) { return stage.key === task.stage; });
      if (currentIndex < 0 && task.status === 'completed') currentIndex = stages.length - 1;
      if (currentIndex < 0) currentIndex = 0;
      container.innerHTML = stages.map(function(stage, index) {
        var state = 'todo';
        if (task.status === 'failed' && index === currentIndex) state = 'failed';
        else if (index < currentIndex) state = 'done';
        else if (index === currentIndex) state = task.status === 'completed' ? 'done' : 'active';
        return '<li class="' + state + '"><span></span><em>' + stage.label + '</em></li>';
      }).join('');
    }
    function runningButtonText(action) {
      if (action === 'run') return tr('running.run');
      if (action === 'generate') return tr('running.generate');
      if (action === 'discover') return tr('running.discover');
      return tr('running.task');
    }
    function setTaskButtons(action, running) {
      document.querySelectorAll('[data-task-action="' + action + '"]').forEach(function(form) {
        form.querySelectorAll('button,input,select,textarea').forEach(function(item) {
          if (item.type === 'hidden') return;
          item.disabled = running;
        });
        form.querySelectorAll('button').forEach(function(button) {
          if (running) {
            if (!button.dataset.originalText) button.dataset.originalText = button.textContent;
            button.textContent = runningButtonText(action);
          } else if (button.dataset.originalText) {
            button.textContent = button.dataset.originalText;
            delete button.dataset.originalText;
          }
        });
      });
    }
    function showTaskPanel(task) {
      var panel = document.querySelector('[data-task-panel]');
      if (!panel || sessionStorage.getItem(taskDismissKey(task)) === '1') return;
      var title = panel.querySelector('[data-task-title]');
      var status = panel.querySelector('[data-task-status]');
      var detail = panel.querySelector('[data-task-detail]');
      var hint = panel.querySelector('[data-task-hint]');
      var steps = panel.querySelector('[data-task-steps]');
      var stream = panel.querySelector('[data-task-stream]');
      var detailText = task.message || task.detail || (task.action === 'run' ? tr('detail.run') : tr('detail.model'));
      if (task.status === 'running' && task.stage === 'llm' && !task.stream_excerpt && task.started_at) {
        var waited = Math.max(0, Math.floor(Date.now() / 1000 - Number(task.started_at)));
        detailText += tr('detail.no_stream', {seconds: waited});
      }
      if (title) title.textContent = task.label || tr('panel.background');
      var active = task.status === 'queued' || task.status === 'running';
      var cancel = panel.querySelector('[data-task-cancel]');
      if (cancel) {
        cancel.hidden = !active;
        cancel.querySelector('[name="task_id"]').value = task.id;
        cancel.querySelector('button').disabled = !!task.cancel_requested;
      }
      if (status) status.textContent = task.status === 'queued' ? tr('status.queued') : (task.status === 'running' ? tr('status.running') : (task.status === 'completed' ? tr('status.completed') : tr('status.failed')));
      if (detail) detail.textContent = detailText;
      if (hint) hint.textContent = active ? (task.status === 'queued' ? tr('hint.queued') : (task.action === 'run' ? tr('hint.run') : tr('hint.model'))) : tr('hint.done');
      renderTaskSteps(task, steps);
      if (stream) {
        if (task.stream_excerpt) {
          stream.hidden = false;
          stream.textContent = task.stream_excerpt;
          stream.scrollTop = stream.scrollHeight;
        } else {
          stream.hidden = true;
          stream.textContent = '';
        }
      }
      panel.dataset.taskId = task.id;
      panel.dataset.taskStatus = task.status;
      if (status && task.status === 'cancelled') status.textContent = tr('status.cancelled');
      panel.hidden = false;
    }
    function maybeReloadForTask(task) {
      if (task.status !== 'completed' && task.status !== 'cancelled') return;
      var relevant = (task.action === 'discover' && window.location.pathname === '/interfaces') || ((task.action === 'generate' || task.action === 'run') && window.location.pathname === '/cases');
      if (!relevant) return;
      var key = 'iatest-task-reloaded-' + task.id;
      if (sessionStorage.getItem(key) === '1') return;
      sessionStorage.setItem(key, '1');
      window.setTimeout(function() {
        var url = new URL(window.location.href);
        url.searchParams.delete('message');
        window.history.replaceState({}, '', url.toString());
        window.location.reload();
      }, 800);
    }
    function pollTasks() {
      fetch('/task-status?project=' + encodeURIComponent(taskProject()), {cache: 'no-store'})
        .then(function(response) { return response.json(); })
        .then(function(payload) {
          var tasks = payload.tasks || [];
          var running = {};
          tasks.forEach(function(task) {
            if (task.status === 'queued' || task.status === 'running') running[task.action] = true;
          });
          setTaskButtons('discover', !!running.discover);
          setTaskButtons('generate', !!running.generate);
          setTaskButtons('run', !!running.run);
          var visibleTask = tasks.find(function(task) { return task.status === 'queued' || task.status === 'running'; }) || tasks[0];
          if (visibleTask) {
            showTaskPanel(visibleTask);
            maybeReloadForTask(visibleTask);
          }
        })
        .catch(function() {});
    }
    function toggleSecretInput(button) {
      var input = button && button.parentElement ? button.parentElement.querySelector('[data-secret-input]') : null;
      if (!input || !input.value) return;
      if (input.type === 'password') {
        input.type = 'text';
        button.textContent = tr('secret.hide');
      } else {
        input.type = 'password';
        button.textContent = tr('secret.show');
      }
      input.focus();
    }
    function updateSecretToggle(input) {
      var button = input && input.parentElement ? input.parentElement.querySelector('[data-secret-toggle]') : null;
      if (!button) return;
      if (!input.value) {
        input.type = 'text';
        button.textContent = tr('secret.hide');
        button.disabled = true;
        return;
      }
      button.disabled = false;
      button.textContent = input.type === 'password' ? tr('secret.show') : tr('secret.hide');
    }
    document.querySelectorAll('[data-secret-input]').forEach(function(input) {
      updateSecretToggle(input);
      input.addEventListener('input', function() {
        updateSecretToggle(input);
      });
    });
    var taskClose = document.querySelector('[data-task-close]');
    if (taskClose) {
      taskClose.addEventListener('click', function() {
        var panel = document.querySelector('[data-task-panel]');
        if (!panel) return;
        if (panel.dataset.taskId) {
          sessionStorage.setItem('iatest-task-dismissed-' + panel.dataset.taskId + '-' + (panel.dataset.taskStatus || 'running'), '1');
        }
        panel.hidden = true;
      });
    }
    pollTasks();
    window.setInterval(pollTasks, 2000);
    document.querySelectorAll('dialog[data-reset-on-close]').forEach(function(dialog) {
      dialog.addEventListener('close', function() {
        resetDialog(dialog);
      });
    });
    document.querySelectorAll('[data-source-edit]').forEach(function(form) {
      updateSourceMode(form);
      var mode = form.querySelector('[data-source-mode]');
      if (mode) {
        mode.addEventListener('change', function() {
          updateSourceMode(form);
        });
      }
    });
    document.querySelectorAll('[data-body-editor]').forEach(function(editor) {
      updateBodyEditor(editor);
      var mode = editor.querySelector('[data-body-mode]');
      if (mode) {
        mode.addEventListener('change', function() {
          updateBodyEditor(editor);
        });
      }
    });
    document.addEventListener('submit', function(event) {
      var form = event.target;
      if (!form || !form.querySelector) return;
      var editor = form.querySelector('[data-body-editor]');
      if (!editor) return;
      var mode = editor.querySelector('[data-body-mode]');
      if (!mode || mode.value !== 'file') {
        form.querySelectorAll('input[name="body_file_name"],input[name="body_file_data"]').forEach(function(input) {
          input.value = '';
        });
        return;
      }
      if (form.dataset.bodyFileReady === '1') {
        delete form.dataset.bodyFileReady;
        return;
      }
      var fileInput = editor.querySelector('[data-body-file]');
      var file = fileInput && fileInput.files ? fileInput.files[0] : null;
      if (!file) return;
      event.preventDefault();
      var reader = new FileReader();
      reader.onload = function() {
        var nameInput = editor.querySelector('input[name="body_file_name"]');
        var dataInput = editor.querySelector('input[name="body_file_data"]');
        if (nameInput) nameInput.value = file.name;
        if (dataInput) dataInput.value = String(reader.result || '');
        form.dataset.bodyFileReady = '1';
        form.requestSubmit();
      };
      reader.onerror = function() {
        alert(tr('error.body_file'));
      };
      reader.readAsDataURL(file);
    }, true);
    document.addEventListener('submit', function(event) {
      var form = event.target;
      if (!form || !form.dataset || form.dataset.sourceEdit === undefined) return;
      var mode = form.querySelector('[data-source-mode]');
      if (mode && mode.value === 'url') {
        var sourceText = form.querySelector('input[name="source_text"]');
        var sourceFilename = form.querySelector('input[name="source_filename"]');
        if (sourceText) sourceText.value = '';
        if (sourceFilename) sourceFilename.value = '';
        return;
      }
      if (form.dataset.fileReady === '1') {
        delete form.dataset.fileReady;
        return;
      }
      var fileInput = form.querySelector('[data-source-file]');
      var file = fileInput && fileInput.files ? fileInput.files[0] : null;
      if (!file) {
        event.preventDefault();
        alert(tr('error.source_required'));
        return;
      }
      event.preventDefault();
      var reader = new FileReader();
      reader.onload = function() {
        var textInput = form.querySelector('input[name="source_text"]');
        var nameInput = form.querySelector('input[name="source_filename"]');
        if (textInput) textInput.value = String(reader.result || '');
        if (nameInput) nameInput.value = file.name;
        form.dataset.fileReady = '1';
        form.requestSubmit();
      };
      reader.onerror = function() {
        alert(tr('error.source_file'));
      };
      reader.readAsText(file);
    }, true);
    document.addEventListener('submit', function(event) {
      if (event.defaultPrevented) return;
      var form = event.target;
      if (!form || !form.dataset || !form.dataset.loadingMessage) return;
      document.body.classList.add('is-busy');
      var title = document.getElementById('loading-title');
      if (title) title.textContent = form.dataset.loadingMessage;
      document.querySelectorAll('button').forEach(function(item) {
        item.disabled = true;
      });
    });

    function bulkSelectionInputs(group) {
      return Array.from(document.querySelectorAll('[data-bulk-select]')).filter(function(input) {
        return input.dataset.bulkSelect === group;
      });
    }
    function updateBulkSelection(group) {
      var inputs = bulkSelectionInputs(group);
      var selectedCount = inputs.filter(function(input) { return input.checked; }).length;
      var selectAll = Array.from(document.querySelectorAll('[data-bulk-select-all]')).find(function(input) {
        return input.dataset.bulkSelectAll === group;
      });
      var count = Array.from(document.querySelectorAll('[data-bulk-selection-count]')).find(function(item) {
        return item.dataset.bulkSelectionCount === group;
      });
      var button = Array.from(document.querySelectorAll('[data-bulk-delete-button]')).find(function(item) {
        return item.dataset.bulkDeleteButton === group;
      });
      if (selectAll) {
        selectAll.checked = inputs.length > 0 && selectedCount === inputs.length;
        selectAll.indeterminate = selectedCount > 0 && selectedCount < inputs.length;
        selectAll.disabled = inputs.length === 0;
      }
      if (count) count.textContent = tr('bulk.selected', {count: selectedCount});
      if (button) button.disabled = selectedCount === 0;
    }
    document.querySelectorAll('[data-bulk-select]').forEach(function(input) {
      input.addEventListener('change', function() {
        updateBulkSelection(input.dataset.bulkSelect);
      });
    });
    document.querySelectorAll('[data-bulk-select-all]').forEach(function(selectAll) {
      var group = selectAll.dataset.bulkSelectAll;
      selectAll.addEventListener('change', function() {
        bulkSelectionInputs(group).forEach(function(input) {
          input.checked = selectAll.checked;
        });
        updateBulkSelection(group);
      });
      updateBulkSelection(group);
    });
    document.querySelectorAll('[data-bulk-delete-form]').forEach(function(form) {
      form.addEventListener('submit', function(event) {
        var group = form.dataset.bulkDeleteForm;
        var selectedCount = bulkSelectionInputs(group).filter(function(input) { return input.checked; }).length;
        if (!selectedCount) {
          event.preventDefault();
          return;
        }
        var label = form.dataset.bulkItemLabel || tr('bulk.content');
        if (!window.confirm(tr('bulk.confirm', {count: selectedCount, label: label}))) {
          event.preventDefault();
        }
      });
    });

    /* Theme toggle */
    (function() {
      var toggle = document.getElementById('theme-toggle');
      if (!toggle) return;
      toggle.addEventListener('click', function() {
        var html = document.documentElement;
        var current = html.getAttribute('data-theme');
        var next = current === 'dark' ? 'light' : 'dark';
        html.setAttribute('data-theme', next);
        try { localStorage.setItem('iatest-theme', next) } catch(e) {}
      });
    })();

    /* Language switch */
    (function() {
      var select = document.querySelector('[data-language-select]');
      if (!select) return;
      select.addEventListener('change', function() {
        document.cookie = 'iatest_lang=' + encodeURIComponent(select.value) + '; Path=/; Max-Age=31536000; SameSite=Lax';
        var url = new URL(window.location.href);
        url.searchParams.delete('lang');
        window.location.assign(url.toString());
      });
    })();
    function clientTranslations() {
      return {
        locale: document.documentElement.dataset.locale || 'en',
        catalog: {
      en: {
        'stage.queued': 'Queued', 'stage.read_document': 'Read document', 'stage.parse_document': 'Parse document',
        'stage.llm_discovery': 'Model discovery', 'stage.audit': 'Quality review', 'stage.save_results': 'Save results',
        'stage.complete': 'Complete', 'stage.read_endpoints': 'Read endpoints', 'stage.prepare': 'Prepare strategy',
        'stage.llm_generate': 'Model generation', 'stage.save_cases': 'Save cases', 'stage.read_cases': 'Read cases',
        'stage.filter': 'Filter scope', 'stage.execute': 'Execute requests', 'stage.audit_report': 'Report and knowledge review',
        'stage.generate_report': 'Generate report', 'running.run': 'Running cases', 'running.generate': 'Generating cases',
        'running.discover': 'Discovering APIs', 'running.task': 'Task in progress', 'detail.run': 'Test cases are running. Please wait.',
        'detail.model': 'The model is generating content. This may take a while.',
        'detail.no_stream': ' (waited {seconds}s; no model output received yet)', 'panel.background': 'Background task',
        'status.queued': 'Waiting', 'status.running': 'Running', 'status.completed': 'Completed', 'status.failed': 'Failed', 'status.cancelled': 'Cancelled',
        'hint.queued': 'The task is in the persistent queue and will continue after service recovery. Another task of the same type cannot start yet.',
        'hint.run': 'Cases are running. You may close this notice; another run cannot start until this one finishes.',
        'hint.model': 'The model is generating content. This may take a while. You may close this notice; another task of the same type cannot start yet.',
        'hint.done': 'The task has ended. The page will show the latest result.', 'secret.show': 'Show', 'secret.hide': 'Hide',
        'error.body_file': 'Failed to read the request body file', 'error.source_required': 'Select a local API document file',
        'error.source_file': 'Failed to read the local file', 'bulk.selected': '{count} selected on this page',
        'bulk.content': 'items', 'bulk.confirm': 'Delete the selected {count} {label}? This cannot be undone.'
      },
      zh: {
        'status.cancelled': '已取消',
        'stage.queued': '排队启动', 'stage.read_document': '读取文档', 'stage.parse_document': '解析文档',
        'stage.llm_discovery': '大模型识别', 'stage.audit': '质量审计', 'stage.save_results': '保存结果',
        'stage.complete': '完成', 'stage.read_endpoints': '读取接口', 'stage.prepare': '准备策略',
        'stage.llm_generate': '大模型生成', 'stage.save_cases': '保存用例', 'stage.read_cases': '读取用例',
        'stage.filter': '筛选范围', 'stage.execute': '执行请求', 'stage.audit_report': '报告与经验审计',
        'stage.generate_report': '生成报告', 'running.run': '用例执行中', 'running.generate': '用例生成中',
        'running.discover': '接口识别中', 'running.task': '任务进行中', 'detail.run': '当前正在执行测试用例，请稍作等待。',
        'detail.model': '当前大模型在生成中，可能所需时间较久，请稍作等待。',
        'detail.no_stream': '（已等待 {seconds} 秒，尚未收到模型内容片段）', 'panel.background': '后台任务',
        'status.queued': '等待执行', 'status.running': '正在执行', 'status.completed': '已完成', 'status.failed': '执行失败',
        'hint.queued': '任务已进入持久队列，服务恢复后会继续处理；同类任务完成前不能重复发起。',
        'hint.run': '当前用例正在执行中。你可以关闭此提示并查看其它功能；执行完成前不能重复发起执行。',
        'hint.model': '当前大模型在生成中，可能所需时间较久，请稍作等待。你可以关闭此提示并查看其它功能；同类任务完成前不能重复发起。',
        'hint.done': '任务已结束，页面会显示最新结果。', 'secret.show': '显示', 'secret.hide': '隐藏',
        'error.body_file': '读取请求体文件失败', 'error.source_required': '请选择本地接口文档文件',
        'error.source_file': '读取本地文件失败', 'bulk.selected': '当前页已选择 {count} 条',
        'bulk.content': '内容', 'bulk.confirm': '确认删除已选择的 {count} 条{label}？此操作不可撤销。'
      },
      ja: {
        'status.cancelled': 'キャンセル済み',
        'stage.queued': '開始待ち', 'stage.read_document': 'ドキュメント読込', 'stage.parse_document': 'ドキュメント解析',
        'stage.llm_discovery': 'モデルによる検出', 'stage.audit': '品質レビュー', 'stage.save_results': '結果を保存',
        'stage.complete': '完了', 'stage.read_endpoints': 'エンドポイント読込', 'stage.prepare': '戦略を準備',
        'stage.llm_generate': 'モデルによる生成', 'stage.save_cases': 'ケースを保存', 'stage.read_cases': 'ケース読込',
        'stage.filter': '範囲を絞り込み', 'stage.execute': 'リクエスト実行', 'stage.audit_report': 'レポートとナレッジのレビュー',
        'stage.generate_report': 'レポート生成', 'running.run': 'ケース実行中', 'running.generate': 'ケース生成中',
        'running.discover': 'API検出中', 'running.task': 'タスク実行中', 'detail.run': 'テストケースを実行しています。お待ちください。',
        'detail.model': 'モデルが生成中です。時間がかかる場合があります。',
        'detail.no_stream': '（{seconds}秒待機中。モデル出力はまだありません）', 'panel.background': 'バックグラウンドタスク',
        'status.queued': '実行待ち', 'status.running': '実行中', 'status.completed': '完了', 'status.failed': '失敗',
        'hint.queued': 'タスクは永続キューに入りました。サービス復旧後も続行します。同種のタスクはまだ開始できません。',
        'hint.run': 'ケースを実行中です。この通知は閉じられますが、完了まで次の実行は開始できません。',
        'hint.model': 'モデルが生成中です。この通知は閉じられますが、同種のタスクはまだ開始できません。',
        'hint.done': 'タスクは終了しました。ページに最新結果が表示されます。', 'secret.show': '表示', 'secret.hide': '非表示',
        'error.body_file': 'リクエスト本文ファイルを読み込めませんでした', 'error.source_required': 'ローカルのAPIドキュメントを選択してください',
        'error.source_file': 'ローカルファイルを読み込めませんでした', 'bulk.selected': 'このページで{count}件選択',
        'bulk.content': '項目', 'bulk.confirm': '選択した{count}件の{label}を削除しますか？この操作は取り消せません。'
      },
      fr: {
        'status.cancelled': 'Annulé',
        'stage.queued': 'En attente', 'stage.read_document': 'Lecture du document', 'stage.parse_document': 'Analyse du document',
        'stage.llm_discovery': 'Découverte par le modèle', 'stage.audit': 'Revue qualité', 'stage.save_results': 'Enregistrement des résultats',
        'stage.complete': 'Terminé', 'stage.read_endpoints': 'Lecture des points de terminaison', 'stage.prepare': 'Préparation de la stratégie',
        'stage.llm_generate': 'Génération par le modèle', 'stage.save_cases': 'Enregistrement des cas', 'stage.read_cases': 'Lecture des cas',
        'stage.filter': 'Filtrage de la portée', 'stage.execute': 'Exécution des requêtes', 'stage.audit_report': 'Revue du rapport et des connaissances',
        'stage.generate_report': 'Génération du rapport', 'running.run': 'Exécution des cas', 'running.generate': 'Génération des cas',
        'running.discover': 'Découverte des API', 'running.task': 'Tâche en cours', 'detail.run': 'Les cas de test sont en cours. Veuillez patienter.',
        'detail.model': 'Le modèle génère du contenu. Cela peut prendre du temps.',
        'detail.no_stream': ' ({seconds} s d’attente ; aucune sortie du modèle)', 'panel.background': 'Tâche en arrière-plan',
        'status.queued': 'En attente', 'status.running': 'En cours', 'status.completed': 'Terminé', 'status.failed': 'Échec',
        'hint.queued': 'La tâche est dans la file persistante et reprendra après le rétablissement du service. Une tâche identique ne peut pas encore démarrer.',
        'hint.run': 'Les cas sont en cours. Vous pouvez fermer ce message ; une autre exécution ne pourra démarrer qu’après la fin.',
        'hint.model': 'Le modèle génère du contenu. Vous pouvez fermer ce message ; une tâche identique ne peut pas encore démarrer.',
        'hint.done': 'La tâche est terminée. La page affichera le dernier résultat.', 'secret.show': 'Afficher', 'secret.hide': 'Masquer',
        'error.body_file': 'Échec de lecture du fichier du corps de requête', 'error.source_required': 'Sélectionnez un document API local',
        'error.source_file': 'Échec de lecture du fichier local', 'bulk.selected': '{count} sélectionné(s) sur cette page',
        'bulk.content': 'éléments', 'bulk.confirm': 'Supprimer les {count} {label} sélectionnés ? Cette action est irréversible.'
      },
      ru: {
        'status.cancelled': 'Отменено',
        'stage.queued': 'В очереди', 'stage.read_document': 'Чтение документа', 'stage.parse_document': 'Разбор документа',
        'stage.llm_discovery': 'Обнаружение моделью', 'stage.audit': 'Проверка качества', 'stage.save_results': 'Сохранение результатов',
        'stage.complete': 'Готово', 'stage.read_endpoints': 'Чтение конечных точек', 'stage.prepare': 'Подготовка стратегии',
        'stage.llm_generate': 'Генерация моделью', 'stage.save_cases': 'Сохранение сценариев', 'stage.read_cases': 'Чтение сценариев',
        'stage.filter': 'Фильтрация области', 'stage.execute': 'Выполнение запросов', 'stage.audit_report': 'Проверка отчёта и знаний',
        'stage.generate_report': 'Создание отчёта', 'running.run': 'Выполнение сценариев', 'running.generate': 'Создание сценариев',
        'running.discover': 'Обнаружение API', 'running.task': 'Задача выполняется', 'detail.run': 'Тестовые сценарии выполняются. Подождите.',
        'detail.model': 'Модель создаёт содержимое. Это может занять некоторое время.',
        'detail.no_stream': ' (ожидание {seconds} с; вывод модели ещё не получен)', 'panel.background': 'Фоновая задача',
        'status.queued': 'Ожидание', 'status.running': 'Выполняется', 'status.completed': 'Завершено', 'status.failed': 'Ошибка',
        'hint.queued': 'Задача находится в постоянной очереди и продолжится после восстановления сервиса. Такая же задача пока не может быть запущена.',
        'hint.run': 'Сценарии выполняются. Уведомление можно закрыть; новый запуск доступен после завершения.',
        'hint.model': 'Модель создаёт содержимое. Уведомление можно закрыть; такая же задача пока не может быть запущена.',
        'hint.done': 'Задача завершена. На странице появится последний результат.', 'secret.show': 'Показать', 'secret.hide': 'Скрыть',
        'error.body_file': 'Не удалось прочитать файл тела запроса', 'error.source_required': 'Выберите локальный документ API',
        'error.source_file': 'Не удалось прочитать локальный файл', 'bulk.selected': 'Выбрано на странице: {count}',
        'bulk.content': 'элементов', 'bulk.confirm': 'Удалить выбранные элементы ({count}, {label})? Это действие нельзя отменить.'
      }
        }
      };
    }
    function tr(key, values) {
      var catalog = CLIENT_I18N[IATEST_LOCALE] || CLIENT_I18N.en;
      var value = catalog[key] || CLIENT_I18N.en[key] || key;
      Object.keys(values || {}).forEach(function(name) {
        value = value.split('{' + name + '}').join(String(values[name]));
      });
      return value;
    }
