import javax.imageio.ImageIO;
import java.awt.*;
import java.awt.image.BufferedImage;
import java.io.File;

/** 整屏截图小工具：java Capture <输出png> */
public class Capture {
    public static void main(String[] args) throws Exception {
        Robot robot = new Robot();
        Rectangle screen = new Rectangle(Toolkit.getDefaultToolkit().getScreenSize());
        BufferedImage image = robot.createScreenCapture(screen);

        ImageIO.write(image, "png", new File(args[0]));
        System.out.println("saved " + args[0]);
    }
}
