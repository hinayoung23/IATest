(() => {
  const data = globalThis.__iatestPerformance = {long_tasks:[],paints:[],largest_paints:[],events:[],supported:PerformanceObserver.supportedEntryTypes || []};
  const observe = (type, field) => {
    if (!data.supported.includes(type)) return;
    try { new PerformanceObserver(list => {
      for (const e of list.getEntries()) {
        if (data[field].length < 512) data[field].push({name:e.name.slice(0,80),start:e.startTime,duration:e.duration});
      }
    }).observe({type,buffered:true,durationThreshold:16}); } catch (_) {}
  };
  observe('longtask','long_tasks'); observe('paint','paints');
  observe('largest-contentful-paint','largest_paints'); observe('event','events');
})()
