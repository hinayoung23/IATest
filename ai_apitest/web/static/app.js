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
        var active = (name === 'schema' && (value === 'json' || value === 'form')) || (name === 'file' && value === 'file');
        section.hidden = !active;
        section.style.display = active ? 'grid' : 'none';
      });
    }
    function taskProject() {
      return new URLSearchParams(window.location.search).get('project') || 'default';
    }
    function taskDismissKey(task) {
      return 'aitest-task-dismissed-' + task.id + '-' + task.status;
    }
    var TASK_STAGES = {
      discover: [
        {key: 'queued', label: '排队启动'},
        {key: 'read', label: '读取文档'},
        {key: 'parse', label: '解析文档'},
        {key: 'llm', label: '大模型识别'},
        {key: 'save', label: '保存结果'},
        {key: 'complete', label: '完成'}
      ],
      generate: [
        {key: 'queued', label: '排队启动'},
        {key: 'read', label: '读取接口'},
        {key: 'prepare', label: '准备策略'},
        {key: 'llm', label: '大模型生成'},
        {key: 'save', label: '保存用例'},
        {key: 'complete', label: '完成'}
      ],
      run: [
        {key: 'queued', label: '排队启动'},
        {key: 'read', label: '读取用例'},
        {key: 'filter', label: '筛选范围'},
        {key: 'execute', label: '执行请求'},
        {key: 'report', label: '生成报告'},
        {key: 'complete', label: '完成'}
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
      if (action === 'run') return '用例执行中';
      if (action === 'generate') return '用例生成中';
      if (action === 'discover') return '接口识别中';
      return '任务进行中';
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
      var detailText = task.message || task.detail || (task.action === 'run' ? '当前正在执行测试用例，请稍作等待。' : '当前大模型在生成中，可能所需时间较久，请稍作等待。');
      if (task.status === 'running' && task.stage === 'llm' && !task.stream_excerpt && task.started_at) {
        var waited = Math.max(0, Math.floor(Date.now() / 1000 - Number(task.started_at)));
        detailText += '（已等待 ' + waited + ' 秒，尚未收到模型内容片段）';
      }
      if (title) title.textContent = task.label || '后台任务';
      if (status) status.textContent = task.status === 'running' ? '正在执行' : (task.status === 'completed' ? '已完成' : '执行失败');
      if (detail) detail.textContent = detailText;
      if (hint) hint.textContent = task.status === 'running' ? (task.action === 'run' ? '当前用例正在执行中。你可以关闭此提示并查看其它功能；执行完成前不能重复发起执行。' : '当前大模型在生成中，可能所需时间较久，请稍作等待。你可以关闭此提示并查看其它功能；同类任务完成前不能重复发起。') : '任务已结束，页面会显示最新结果。';
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
      panel.hidden = false;
    }
    function maybeReloadForTask(task) {
      if (task.status !== 'completed') return;
      var relevant = (task.action === 'discover' && window.location.pathname === '/interfaces') || ((task.action === 'generate' || task.action === 'run') && window.location.pathname === '/cases');
      if (!relevant) return;
      var key = 'aitest-task-reloaded-' + task.id;
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
            if (task.status === 'running') running[task.action] = true;
          });
          setTaskButtons('discover', !!running.discover);
          setTaskButtons('generate', !!running.generate);
          setTaskButtons('run', !!running.run);
          var visibleTask = tasks.find(function(task) { return task.status === 'running'; }) || tasks[0];
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
        button.textContent = '隐藏';
      } else {
        input.type = 'password';
        button.textContent = '显示';
      }
      input.focus();
    }
    function updateSecretToggle(input) {
      var button = input && input.parentElement ? input.parentElement.querySelector('[data-secret-toggle]') : null;
      if (!button) return;
      if (!input.value) {
        input.type = 'text';
        button.textContent = '隐藏';
        button.disabled = true;
        return;
      }
      button.disabled = false;
      button.textContent = input.type === 'password' ? '显示' : '隐藏';
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
          sessionStorage.setItem('aitest-task-dismissed-' + panel.dataset.taskId + '-' + (panel.dataset.taskStatus || 'running'), '1');
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
        alert('读取请求体文件失败');
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
        alert('请选择本地接口文档文件');
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
        alert('读取本地文件失败');
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

    /* Theme toggle */
    (function() {
      var toggle = document.getElementById('theme-toggle');
      if (!toggle) return;
      toggle.addEventListener('click', function() {
        var html = document.documentElement;
        var current = html.getAttribute('data-theme');
        var next = current === 'dark' ? 'light' : 'dark';
        html.setAttribute('data-theme', next);
        try { localStorage.setItem('aitest-theme', next) } catch(e) {}
      });
    })();
