/**
 * Local Catchup Plugin - Frontend Toggle Injection
 *
 * Injects a "Local Catchup" toggle switch into the Channel edit form.
 * Uses MutationObserver to detect when the modal appears, finds the
 * "Mature Content" switch, and adds our toggle after it.
 *
 * The toggle value is persisted via the ChannelSerializer monkey-patch:
 * - Read: local_catchup field in channel API responses (captured via fetch intercept)
 * - Write: local_catchup field included in PATCH requests via fetch intercept
 */
(function () {
  'use strict';

  if (window.__localCatchupInjected) return;
  window.__localCatchupInjected = true;
  console.log('[LocalCatchup] inject.js loaded');

  // Track local_catchup state per channel ID (string keys)
  var catchupState = {};

  // Single fetch interceptor for both reading responses and injecting into PATCH
  var originalFetch = window.fetch;
  window.fetch = function (url, options) {
    var isChannelApi =
      typeof url === 'string' && url.includes('/api/channels/channels');

    // Inject local_catchup into PATCH requests
    if (
      options &&
      options.method === 'PATCH' &&
      isChannelApi &&
      url.match(/\/api\/channels\/channels\/\d+\//)
    ) {
      try {
        var body = JSON.parse(options.body);
        var idMatch = url.match(/\/api\/channels\/channels\/(\d+)\//);
        if (idMatch && idMatch[1] in catchupState) {
          body.local_catchup = catchupState[idMatch[1]];
          options = Object.assign({}, options, {
            body: JSON.stringify(body),
          });
          console.log('[LocalCatchup] Injected local_catchup=' + body.local_catchup + ' into PATCH for channel ' + idMatch[1]);
        }
      } catch (e) {
        // Not JSON or parse error, pass through
      }
    }

    var promise = originalFetch.call(this, url, options);

    // Capture local_catchup from channel API responses
    if (isChannelApi && (!options || !options.method || options.method === 'GET' || options.method === 'PATCH')) {
      promise.then(function (response) {
        var cloned = response.clone();
        cloned
          .json()
          .then(function (data) {
            var items = [];
            if (Array.isArray(data)) {
              items = data;
            } else if (data && data.results && Array.isArray(data.results)) {
              items = data.results;
            } else if (data && data.id !== undefined) {
              items = [data];
            }
            var count = 0;
            items.forEach(function (ch) {
              if (ch.id !== undefined && ch.local_catchup !== undefined) {
                catchupState[String(ch.id)] = ch.local_catchup;
                count++;
              }
            });
            if (count > 0) {
              console.log('[LocalCatchup] Captured local_catchup state for ' + count + ' channels');
            }
          })
          .catch(function () {});
        return response;
      }).catch(function () {});
    }

    return promise;
  };

  /**
   * Find a Switch element by its label text within a container.
   */
  function findSwitchByLabel(container, labelText) {
    var labels = container.querySelectorAll('label, span');
    for (var i = 0; i < labels.length; i++) {
      if (labels[i].textContent && labels[i].textContent.trim() === labelText) {
        var el = labels[i];
        // Prefer Mantine switch root if present
        if (el.closest) {
          var root = el.closest('.mantine-Switch-root');
          if (root) {
            var input = root.querySelector('input[role="switch"]');
            if (input) return { container: root, input: input };
          }
        }
        // Walk up to find the nearest ancestor that contains a switch input
        for (var j = 0; j < 10; j++) {
          el = el.parentElement;
          if (!el) break;
          var switchInput = el.querySelector('input[role="switch"]');
          if (switchInput) {
            return { container: el, input: switchInput };
          }
        }
      }
    }
    // Fallback: Mantine-specific label class
    var mantineLabels = container.querySelectorAll('.mantine-Switch-label');
    for (var k = 0; k < mantineLabels.length; k++) {
      var lbl = mantineLabels[k];
      if (lbl.textContent && lbl.textContent.trim() === labelText) {
        var root2 = lbl.closest && lbl.closest('.mantine-Switch-root');
        if (root2) {
          var input2 = root2.querySelector('input[role="switch"]');
          if (input2) return { container: root2, input: input2 };
        }
      }
    }
    return null;
  }

  /**
   * Find the channel object by traversing React fiber tree from any element.
   * Walks both up (return) and checks stateNode/memoizedState for channel data.
   */
  function findChannelFromFiber(element) {
    try {
      var fiberKey = Object.keys(element).find(function (k) {
        return (
          k.startsWith('__reactFiber$') ||
          k.startsWith('__reactInternalInstance$')
        );
      });
      if (!fiberKey) {
        console.log('[LocalCatchup] No React fiber found on element');
        return null;
      }

      var fiber = element[fiberKey];
      var current = fiber;
      var maxDepth = 100;

      while (current && maxDepth-- > 0) {
        // Check memoizedProps
        var props = current.memoizedProps;
        if (props) {
          if (props.channel && props.channel.id !== undefined) {
            console.log('[LocalCatchup] Found channel in memoizedProps:', props.channel.id, props.channel.name);
            return props.channel;
          }
        }

        // Check pendingProps
        var pending = current.pendingProps;
        if (pending) {
          if (pending.channel && pending.channel.id !== undefined) {
            console.log('[LocalCatchup] Found channel in pendingProps:', pending.channel.id);
            return pending.channel;
          }
        }

        // Check memoizedState for hooks (useState, useRef, etc.)
        var state = current.memoizedState;
        while (state) {
          if (state.memoizedState && typeof state.memoizedState === 'object') {
            var ms = state.memoizedState;
            // Check for channel in state value
            if (ms && ms.id !== undefined && ms.name !== undefined && ms.uuid !== undefined) {
              console.log('[LocalCatchup] Found channel in memoizedState:', ms.id, ms.name);
              return ms;
            }
            // Check for current ref value
            if (ms.current && ms.current.id !== undefined && ms.current.name !== undefined) {
              console.log('[LocalCatchup] Found channel in ref:', ms.current.id);
              return ms.current;
            }
          }
          // useState stores value in queue.lastRenderedState
          if (state.queue && state.queue.lastRenderedState) {
            var lrs = state.queue.lastRenderedState;
            if (lrs && typeof lrs === 'object' && lrs.id !== undefined && lrs.name !== undefined && lrs.uuid !== undefined) {
              console.log('[LocalCatchup] Found channel in queue.lastRenderedState:', lrs.id, lrs.name);
              return lrs;
            }
          }
          state = state.next;
        }

        current = current.return;
      }
    } catch (e) {
      console.log('[LocalCatchup] Fiber traversal error:', e);
    }
    return null;
  }

  /**
   * Try to find the channel by reading the form's name input and looking up in catchupState.
   * Fallback approach when fiber traversal fails.
   */
  function findChannelIdFromForm(modal) {
    // Look for the name input in the form
    var nameInput = modal.querySelector('input[id="name"], input[name="name"]');
    if (!nameInput || !nameInput.value) return null;

    var channelName = nameInput.value;

    // Also try to find channel number input for more precise matching
    var numInput = modal.querySelector('input[id="channel_number"], input[name="channel_number"]');
    var channelNum = numInput ? numInput.value : null;

    // Search catchupState keys - we need to find by name from the store
    // Since we may not have access to the store directly, look for any
    // element in the DOM that might have the channel data
    // Try to find it via the table rows or any data attribute
    console.log('[LocalCatchup] Trying to find channel by form name:', channelName);

    // Try accessing Zustand store via __NEXT_DATA__ or window.__zustand
    // Zustand stores expose getState() but we need to find the store reference
    // Look through all fiber nodes for the channels store
    try {
      var forms = modal.querySelectorAll('form');
      for (var f = 0; f < forms.length; f++) {
        var form = forms[f];
        var fiberKey = Object.keys(form).find(function (k) {
          return k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$');
        });
        if (!fiberKey) continue;

        var fiber = form[fiberKey];
        var current = fiber;
        var maxDepth = 100;

        while (current && maxDepth-- > 0) {
          var props = current.memoizedProps;
          if (props) {
            if (props.channel && props.channel.id !== undefined) {
              console.log('[LocalCatchup] Found channel via form fiber:', props.channel.id);
              return props.channel;
            }
          }
          var pending = current.pendingProps;
          if (pending) {
            if (pending.channel && pending.channel.id !== undefined) {
              console.log('[LocalCatchup] Found channel via form pendingProps:', pending.channel.id);
              return pending.channel;
            }
          }
          // Check useState hooks for channel state
          var state = current.memoizedState;
          while (state) {
            if (state.queue && state.queue.lastRenderedState) {
              var lrs = state.queue.lastRenderedState;
              if (lrs && typeof lrs === 'object' && lrs.id !== undefined && lrs.uuid !== undefined) {
                console.log('[LocalCatchup] Found channel in form state:', lrs.id, lrs.name);
                return lrs;
              }
            }
            if (state.memoizedState && typeof state.memoizedState === 'object') {
              var ms = state.memoizedState;
              if (ms && ms.id !== undefined && ms.uuid !== undefined) {
                return ms;
              }
            }
            state = state.next;
          }
          current = current.return;
        }
      }
    } catch (e) {
      console.log('[LocalCatchup] Form fiber search error:', e);
    }

    return null;
  }

  /**
   * Update the visual state of a Mantine Switch.
   */
  function updateSwitchVisual(wrapper, input, isChecked) {
    var track =
      wrapper.querySelector('[class*="track"]') || input.parentElement;
    var thumb = wrapper.querySelector('[class*="thumb"]');
    var root = wrapper.classList ? wrapper : wrapper.closest && wrapper.closest('.mantine-Switch-root');

    if (isChecked) {
      if (root) root.setAttribute('data-checked', 'true');
      if (track) track.setAttribute('data-checked', '');
      if (thumb) thumb.setAttribute('data-checked', '');
      input.checked = true;
      input.setAttribute('aria-checked', 'true');
    } else {
      if (root) root.removeAttribute('data-checked');
      if (track) track.removeAttribute('data-checked');
      if (thumb) thumb.removeAttribute('data-checked');
      input.checked = false;
      input.setAttribute('aria-checked', 'false');
    }
  }

  /**
   * Create and inject the Local Catchup toggle switch.
   */
  function injectToggle(matureSwitch, channel) {
    var parent = matureSwitch.container.parentElement;
    if (!parent) return;

    // Prevent duplicate injection anywhere within the same modal/root
    var modalRoot = matureSwitch.container.closest('[role="dialog"], .mantine-Modal-content, .mantine-Modal-body, [class*="mantine-Modal"]');
    if (modalRoot && modalRoot.querySelector('[data-local-catchup="true"]')) {
      return;
    }

    // Already injected?
    if (parent.querySelector('[data-local-catchup]')) return;

    var channelId = channel ? String(channel.id) : null;
    console.log('[LocalCatchup] Injecting toggle for channel:', channelId, channel ? channel.name : 'unknown');

    // Initialize state from API data only
    if (channelId && !(channelId in catchupState)) {
      if (channel && channel.local_catchup !== undefined) {
        catchupState[channelId] = !!(channel.local_catchup);
      } else {
        catchupState[channelId] = false;
      }
    }

    var isEnabled = channelId ? !!(catchupState[channelId]) : false;

    // Clone the Mature Content switch structure for consistent styling
    var wrapper = matureSwitch.container.cloneNode(true);
    wrapper.setAttribute('data-local-catchup', 'true');
    wrapper.title = 'Enable local recording and catchup for this channel';

    // Ensure the cloned switch does NOT link to the original input
    var clonedLabel = wrapper.querySelector('label');
    if (clonedLabel) {
      clonedLabel.removeAttribute('for');
    }

    // Find and configure the cloned switch input
    var switchInput = wrapper.querySelector('input[role="switch"]');
    if (switchInput) {
      // Make sure the cloned input is unique and not linked to the original
      switchInput.removeAttribute('name');
      switchInput.id = 'local-catchup-switch-' + String(Date.now()) + '-' + Math.floor(Math.random() * 10000);

      // If Mantine generated a label "for", point it to our new ID
      if (clonedLabel) {
        clonedLabel.setAttribute('for', switchInput.id);
      }

      // Remove any existing Mantine-generated IDs to avoid conflicts
      // (already handled above)

    // Set initial visual state
    updateSwitchVisual(wrapper, switchInput, isEnabled);

      function setValue(newValue) {
        if (!channelId) return;
        catchupState[channelId] = newValue;
        updateSwitchVisual(wrapper, switchInput, newValue);
        // Dispatch change event so any listeners can react
        try {
          var evt = new Event('change', { bubbles: true });
          switchInput.dispatchEvent(evt);
        } catch (e) {}
        console.log('[LocalCatchup] Toggle changed for ch' + channelId + ': ' + newValue);
      }

      function toggleFromEvent(e) {
        if (e) {
          e.preventDefault();
          e.stopPropagation();
        }
        setValue(!catchupState[channelId]);
      }

      // Click handler on input
      switchInput.addEventListener('click', toggleFromEvent);

      // Click handler on wrapper to catch label/track clicks
      wrapper.addEventListener('click', toggleFromEvent);
    }

    // Update the visible label text and keep Mantine label structure
    var labelWrapper = wrapper.querySelector('.mantine-Switch-labelWrapper');
    var existingLabelSpan = wrapper.querySelector('.mantine-Switch-label');
    var labelClass = existingLabelSpan ? existingLabelSpan.className : 'mantine-Switch-label';
    if (labelWrapper) {
      // Build label span explicitly (avoid innerHTML quirks)
      while (labelWrapper.firstChild) {
        labelWrapper.removeChild(labelWrapper.firstChild);
      }
      var labelSpan = document.createElement('span');
      labelSpan.className = labelClass;
      labelSpan.textContent = 'Local Catchup';
      labelWrapper.appendChild(labelSpan);
    } else {
      var labelSpans = wrapper.querySelectorAll('.mantine-Switch-label, [class*="Switch-label"], span');
      for (var j = 0; j < labelSpans.length; j++) {
        var node2 = labelSpans[j];
        if (node2.tagName === 'LABEL') continue;
        if (node2.textContent && node2.textContent.trim() === 'Mature Content') {
          node2.textContent = 'Local Catchup';
        }
      }
    }

    // Insert after the Mature Content switch in its own container (as a sibling)
    var outer = document.createElement('div');
    outer.appendChild(wrapper);
    var outerParent = parent.parentElement || parent;
    var insertAfter = parent;
    if (outerParent && insertAfter) {
      outerParent.insertBefore(outer, insertAfter.nextSibling);
    } else {
      parent.appendChild(outer);
    }
    console.log('[LocalCatchup] Toggle injected successfully');
  }

  /**
   * Process a modal element to inject the toggle if applicable.
   */
  function processModal(modal) {
    var matureSwitch = findSwitchByLabel(modal, 'Mature Content');
    if (!matureSwitch) return;

    var parent = matureSwitch.container.parentElement;
    if (parent && parent.querySelector('[data-local-catchup]')) return;

    console.log('[LocalCatchup] Found Mature Content switch, searching for channel data...');

    // Try fiber traversal from the dialog element
    var channel = findChannelFromFiber(modal);

    // If not found, try from the form element
    if (!channel) {
      channel = findChannelIdFromForm(modal);
    }

    if (!channel) {
      // Retry after a delay - React may still be rendering
      console.log('[LocalCatchup] Channel not found yet, will retry...');
      setTimeout(function () {
        if (parent && parent.querySelector('[data-local-catchup]')) return;
        var ch = findChannelFromFiber(modal);
        if (!ch) ch = findChannelIdFromForm(modal);
        if (ch) {
          var ms = findSwitchByLabel(modal, 'Mature Content');
          if (ms) injectToggle(ms, ch);
        } else {
          console.log('[LocalCatchup] Channel still not found after retry');
          // Last resort: retry one more time with longer delay
          setTimeout(function () {
            if (parent && parent.querySelector('[data-local-catchup]')) return;
            var ch2 = findChannelFromFiber(modal);
            if (!ch2) ch2 = findChannelIdFromForm(modal);
            if (ch2) {
              var ms2 = findSwitchByLabel(modal, 'Mature Content');
              if (ms2) injectToggle(ms2, ch2);
            } else {
              console.log('[LocalCatchup] Giving up finding channel data');
            }
          }, 1000);
        }
      }, 500);
      return;
    }

    injectToggle(matureSwitch, channel);
  }

  /**
   * Scan the DOM for channel edit modals.
   */
  function scanForModals() {
    // Mantine modals render in portals - check both dialog and modal-specific selectors
    var modals = document.querySelectorAll(
      '[role="dialog"], .mantine-Modal-content, .mantine-Modal-body, [class*="mantine-Modal"]'
    );
    if (modals.length > 0) {
      console.log('[LocalCatchup] Found ' + modals.length + ' modal candidates');
    }
    modals.forEach(function (modal) {
      processModal(modal);
    });
  }

  // MutationObserver to watch for modal appearance
  var scanTimeout;
  var observer = new MutationObserver(function (mutations) {
    for (var i = 0; i < mutations.length; i++) {
      if (mutations[i].addedNodes.length > 0) {
        clearTimeout(scanTimeout);
        scanTimeout = setTimeout(scanForModals, 150);
        break;
      }
    }
  });

  observer.observe(document.body, {
    childList: true,
    subtree: true,
  });
})();
