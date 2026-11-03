package dev.iatest.mobile.hid;

import android.content.SharedPreferences;
import java.util.*;

/** Subscription memory lasts exactly as long as the separately owned GATT server. */
final class OwnerPreferences implements SharedPreferences {
    private final Map<String,Object> values=new HashMap<>();
    public synchronized Map<String,?> getAll(){return new HashMap<>(values);}
    public synchronized String getString(String key,String fallback){return values.containsKey(key)?(String)values.get(key):fallback;}
    @SuppressWarnings("unchecked") public synchronized Set<String> getStringSet(String key,Set<String> fallback){return values.containsKey(key)?new HashSet<>((Set<String>)values.get(key)):fallback;}
    public synchronized int getInt(String key,int fallback){return values.containsKey(key)?(Integer)values.get(key):fallback;}
    public synchronized long getLong(String key,long fallback){return values.containsKey(key)?(Long)values.get(key):fallback;}
    public synchronized float getFloat(String key,float fallback){return values.containsKey(key)?(Float)values.get(key):fallback;}
    public synchronized boolean getBoolean(String key,boolean fallback){return values.containsKey(key)?(Boolean)values.get(key):fallback;}
    public synchronized boolean contains(String key){return values.containsKey(key);}
    public void registerOnSharedPreferenceChangeListener(OnSharedPreferenceChangeListener listener){}
    public void unregisterOnSharedPreferenceChangeListener(OnSharedPreferenceChangeListener listener){}
    public Editor edit(){return new Update();}
    private final class Update implements Editor {
        private final Map<String,Object> pending=new HashMap<>();private boolean clear;
        public Editor putString(String key,String value){pending.put(key,value);return this;}
        public Editor putStringSet(String key,Set<String> value){pending.put(key,value==null?null:new HashSet<>(value));return this;}
        public Editor putInt(String key,int value){pending.put(key,value);return this;}
        public Editor putLong(String key,long value){pending.put(key,value);return this;}
        public Editor putFloat(String key,float value){pending.put(key,value);return this;}
        public Editor putBoolean(String key,boolean value){pending.put(key,value);return this;}
        public Editor remove(String key){pending.put(key,null);return this;}
        public Editor clear(){clear=true;return this;}
        public boolean commit(){synchronized(OwnerPreferences.this){if(clear)values.clear();for(Map.Entry<String,Object> e:pending.entrySet())if(e.getValue()==null)values.remove(e.getKey());else values.put(e.getKey(),e.getValue());}return true;}
        public void apply(){commit();}
    }
}
