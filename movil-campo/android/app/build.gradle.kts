import java.util.Properties

plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// Credenciales de firma del release. Fuera de git (ver android/.gitignore): quien tenga
// el keystore puede publicar una actualizacion que los telefonos de los tecnicos van a
// aceptar como legitima.
val archivoFirma = rootProject.file("key.properties")
val propsFirma = Properties().apply {
    if (archivoFirma.exists()) archivoFirma.inputStream().use { load(it) }
}

// Sin key.properties no se firma nada, pero tampoco se rompe un build de depuracion: el
// error salta solo si la tarea que corre es de release. Antes esto caia en las llaves de
// depuracion, que producen un APK que parece publicable y no lo es -- Android lo trata
// como otra app distinta y no puede actualizar a la firmada.
if (!archivoFirma.exists()) {
    gradle.taskGraph.whenReady {
        if (allTasks.any { it.name.contains("Release") }) {
            throw GradleException(
                "Falta movil-campo/android/key.properties: el release no se puede firmar. " +
                "Ver movil-campo/README.md, seccion \"Compilar el release firmado\"."
            )
        }
    }
}

android {
    namespace = "com.cresio.cresio_campo"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // Cambiarlo despues de distribuir el APK obliga a desinstalar y reinstalar en cada
        // telefono: para Android seria otra app. Es tambien el nombre de paquete que hay que
        // registrar en Firebase el dia que se active el push.
        applicationId = "com.cresio.cresio_campo"
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        if (archivoFirma.exists()) {
            create("release") {
                keyAlias = propsFirma.getProperty("keyAlias")
                keyPassword = propsFirma.getProperty("keyPassword")
                // Relativo a android/, no a android/app/, que es donde vive este archivo.
                storeFile = rootProject.file(propsFirma.getProperty("storeFile"))
                storePassword = propsFirma.getProperty("storePassword")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.findByName("release")
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
