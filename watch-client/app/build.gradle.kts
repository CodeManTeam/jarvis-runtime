plugins {
    id("com.android.application")
}

val configuredDeviceToken = providers.gradleProperty("jarvisDeviceToken")
    .orElse(providers.environmentVariable("JARVIS_DEVICE_TOKEN"))
    .orElse("")
    .get()
val escapedDeviceToken = configuredDeviceToken
    .replace("\\", "\\\\")
    .replace("\"", "\\\"")

android {
    namespace = "com.jarvis.watch"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.tencent.mobileqq"
        minSdk = 24
        targetSdk = 28
        versionCode = 1
        versionName = "0.1.0"
        // Configure your own endpoint in watch-client/gradle.properties:
        //   jarvisServerUrl = https://your-host
        // The default targets a local emulator so the repo builds anywhere.
        buildConfigField(
            "String",
            "DEFAULT_SERVER_URL",
            "\"${project.findProperty("jarvisServerUrl") ?: "http://10.0.2.2:8787"}\"",
        )
        buildConfigField("String", "DEFAULT_DEVICE_TOKEN", "\"$escapedDeviceToken\"")
    }

    buildTypes {
        debug {
            if (providers.gradleProperty("jarvisTestPackage").orNull == "true") {
                applicationIdSuffix = ".jarvis_test"
            }
        }
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    testImplementation("junit:junit:4.13.2")
}

