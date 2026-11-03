package dev.iatest.mobile;

import android.app.UiAutomation;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.graphics.Point;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.os.Build;
import android.os.SystemClock;
import android.view.Display;
import android.view.Surface;
import java.lang.reflect.InvocationTargetException;
import java.nio.ByteBuffer;

/** Selected-display pixels. Never substitutes the default display on failure. */
final class DisplayCapture {
    private DisplayCapture() {}

    static synchronized VirtualDisplay mirror(UiAutomation automation, Context context,
            String name, int width, int height, int displayId, Surface surface) throws Exception {
        if (Build.VERSION.SDK_INT < 29) throw new IllegalStateException("Selected display capture requires Android 10+");
        DisplayManager manager = (DisplayManager) context.getSystemService(Context.DISPLAY_SERVICE);
        if (manager.getDisplay(displayId) == null) throw new IllegalStateException("Selected display disappeared");
        automation.adoptShellPermissionIdentity("android.permission.CAPTURE_VIDEO_OUTPUT");
        try {
            java.lang.reflect.Method method = DisplayManager.class.getDeclaredMethod(
                "createVirtualDisplay", String.class, int.class, int.class, int.class, Surface.class);
            method.setAccessible(true);
            VirtualDisplay display = (VirtualDisplay) method.invoke(null, name, width, height, displayId, surface);
            if (display == null) throw new IllegalStateException("Selected display mirror unavailable");
            return display;
        } catch (InvocationTargetException error) {
            throw new IllegalStateException("Selected display mirror rejected", error.getCause());
        } finally {
            automation.dropShellPermissionIdentity();
        }
    }

    static Bitmap capture(UiAutomation automation, Context context, int displayId, long deadline) throws Exception {
        if (displayId == Display.DEFAULT_DISPLAY) {
            Bitmap bitmap = automation.takeScreenshot();
            if (bitmap == null) throw new IllegalStateException("Screenshot unavailable or protected");
            return bitmap;
        }
        DisplayManager manager = (DisplayManager) context.getSystemService(Context.DISPLAY_SERVICE);
        Display display = manager.getDisplay(displayId);
        if (display == null) throw new IllegalStateException("Selected display disappeared");
        Point size = new Point();
        display.getRealSize(size);
        int rotation = display.getRotation();
        if (size.x <= 0 || size.y <= 0 || (long) size.x * size.y > 16777216)
            throw new IllegalStateException("Selected display exceeds pixel quota");
        ImageReader reader = ImageReader.newInstance(size.x, size.y, PixelFormat.RGBA_8888, 2);
        VirtualDisplay mirror = null;
        try {
            mirror = mirror(automation, context, "IATest selected screenshot", size.x, size.y, displayId, reader.getSurface());
            Image image = null;
            try {
                while (image == null) {
                    ControlServer.check(deadline);
                    image = reader.acquireLatestImage();
                    if (image == null) SystemClock.sleep(10);
                }
                Display current = manager.getDisplay(displayId);
                Point actual = new Point();
                if (current == null) throw new IllegalStateException("Selected display disappeared during capture");
                current.getRealSize(actual);
                if (!actual.equals(size) || current.getRotation() != rotation)
                    throw new IllegalStateException("Selected display changed during capture");
                Image.Plane[] planes = image.getPlanes();
                if (planes.length != 1 || planes[0].getPixelStride() != 4 || image.getWidth() != size.x || image.getHeight() != size.y)
                    throw new IllegalStateException("Unsupported selected display pixel layout");
                ByteBuffer pixels = planes[0].getBuffer();
                int rowStride = planes[0].getRowStride();
                if (rowStride < size.x * 4 || rowStride > (size.x + 4096) * 4)
                    throw new IllegalStateException("Selected display row stride exceeds quota");
                ByteBuffer packed = ByteBuffer.allocate(size.x * size.y * 4);
                for (int row = 0; row < size.y; row++) {
                    ControlServer.check(deadline);
                    ByteBuffer line = pixels.duplicate();
                    line.position(row * rowStride);
                    line.limit(row * rowStride + size.x * 4);
                    packed.put(line);
                }
                packed.flip();
                Bitmap bitmap = Bitmap.createBitmap(size.x, size.y, Bitmap.Config.ARGB_8888);
                try { bitmap.copyPixelsFromBuffer(packed); }
                catch (RuntimeException error) { bitmap.recycle(); throw error; }
                return bitmap;
            } finally {
                if (image != null) image.close();
            }
        } finally {
            if (mirror != null) mirror.release();
            reader.close();
        }
    }
}
